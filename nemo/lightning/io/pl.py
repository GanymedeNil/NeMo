import logging

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Generic, Optional, Protocol, TypeVar, Union

import pytorch_lightning as pl
import torch
from lightning_fabric.plugins.io.checkpoint_io import CheckpointIO
from lightning_fabric.utilities.cloud_io import get_filesystem
from lightning_fabric.utilities.types import _PATH
from torch import nn
from typing_extensions import Self, override

from nemo.lightning.io.capture import IOProtocol
from nemo.lightning.io.mixin import IOMixin
from nemo.utils.callbacks.dist_ckpt_io import AsyncCompatibleCheckpointIO
from nemo.utils.callbacks.torch_dist_async import TorchDistAsyncSaveShardedStrategy
from megatron.core import mpu
import os

from nemo.utils.callbacks.torch_dist_async import TorchDistAsyncSaveShardedStrategy
from megatron.core.dist_checkpointing.strategies.zarr import ZarrSaveShardedStrategy

if TYPE_CHECKING:
    from nemo.lightning.pytorch.strategies import MegatronStrategy

log = logging.getLogger(__name__)


LightningModuleT = TypeVar("LightningModuleT", bound=pl.LightningModule)
ModuleT = TypeVar("ModuleT", bound=nn.Module)


@dataclass
class TrainerCheckpoint(IOMixin, Generic[LightningModuleT]):
    model: LightningModuleT
    trainer: pl.Trainer
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_strategy(cls, strategy: "MegatronStrategy") -> Self:
        if not isinstance(strategy.trainer, IOProtocol):
            raise ValueError(f"Trainer must be an instance of {IOProtocol}. Please use the Trainer from nemo.")

        if not isinstance(strategy.lightning_module, IOProtocol):
            raise ValueError("LightningModule must extend IOMixin.")

        return cls(trainer=strategy.trainer, model=strategy.lightning_module, extra=cls.construct_extra(strategy))

    @classmethod
    def construct_extra(cls, strategy: "MegatronStrategy") -> Dict[str, Any]:
        extra = {}
        if hasattr(strategy.trainer, "datamodule") and isinstance(strategy.trainer.datamodule, IOProtocol):
            extra["datamodule"] = strategy.trainer.datamodule.__io__

        # TODO: Add optimizer to extra

        return extra


class TrainerCkptProtocol(Protocol):
    @classmethod
    def from_strategy(cls, strategy: "MegatronStrategy") -> Self: ...

    def io_dump(self, output: Path): ...


class MegatronCheckpointIO(AsyncCompatibleCheckpointIO):
    """CheckpointIO that utilizes :func:`torch.save` and :func:`torch.load` to save and load checkpoints respectively,
    common for most use cases.

    .. warning::  This is an :ref:`experimental <versioning:Experimental API>` feature.

    """

    def __init__(
        self,
        save_ckpt_format: str = 'zarr',
        load_directly_on_device: bool = True, ## TODO: support
        async_save: bool = False,
    ):
        self.save_ckpt_format = save_ckpt_format
        self.load_directly_on_device = load_directly_on_device
        self.async_save = async_save
        self.save_sharded_strategy = self._determine_dist_ckpt_save_strategy()

    @override
    def save_checkpoint(self, checkpoint: Dict[str, Any], path: _PATH, storage_options: Optional[Any] = None) -> None:
        """Save model/training states as a checkpoint file through state-dump and file-write.

        Args:
            checkpoint: dict containing model and trainer state
            path: write-target path
            storage_options: not used in ``TorchCheckpointIO.save_checkpoint``

        Raises
        ------
            TypeError:
                If ``storage_options`` arg is passed in

        """
        from megatron.core import dist_checkpointing

        if storage_options is not None:
            logging.warning(f"{self.__class__.__name__} does not support"
                            f" storage_options, but {storage_options=} was provided."
                            f" Ignoring given storage_options")
        checkpoint_dir = ckpt_to_dir(path)
        fs = get_filesystem(checkpoint_dir)
        if fs.isdir(checkpoint_dir) and dist_checkpointing.check_is_distributed_checkpoint(checkpoint_dir):
            logging.info(f'Distributed checkpoint at path {checkpoint_dir} already exists, skipping saving')
            return
        fs.makedirs(checkpoint_dir, exist_ok=True)

        ## TODO: compare with nemo 1.0
        dist_checkpointing.save(
            checkpoint,
            checkpoint_dir=str(checkpoint_dir),
            sharded_strategy=self.save_sharded_strategy,
        )
        if not self.async_save:
            return None
        # NOTE: this logic will be simplified in MCore v0.7
        assert self.save_sharded_strategy.async_request is not None
        async_request = self.save_sharded_strategy.async_request
        self.save_sharded_strategy.async_request = None
        return async_request

    @override
    def load_checkpoint(
        self, path: _PATH, sharded_state_dict=None, map_location: Optional[Callable] = None
    ) -> Dict[str, Any]:
        """Loads checkpoint using :func:`torch.load`, with additional handling for ``fsspec`` remote loading of files.

        Args:
            path: Path to checkpoint
            map_location: a function, :class:`torch.device`, string or a dict specifying how to remap storage
                locations.

        Returns: The loaded checkpoint.

        Raises
        ------
            FileNotFoundError: If ``path`` is not found by the ``fsspec`` filesystem

        """
        from megatron.core import dist_checkpointing

        if map_location is not None:
            raise ValueError("`map_location` argument is not supported for `MegatronCheckpointIO.load_checkpoint`.")

        # Try to read the checkpoint at `path`. If not exist, do not restore checkpoint.
        fs = get_filesystem(path)
        if not fs.exists(path):
            raise FileNotFoundError(f"Checkpoint file not found: {path}")
        if not fs.isdir(path):
            raise ValueError(f"Distributed checkpoints should be a directory. Found: {path}.")

        # return pl_load(path, map_location=map_location)

        checkpoint = dist_checkpointing.load(sharded_state_dict=sharded_state_dict, checkpoint_dir=str(path))
        checkpoint = _fix_tensors_device(checkpoint)

        return checkpoint

    @override
    def remove_checkpoint(self, path: _PATH) -> None:
        """Remove checkpoint file from the filesystem.

        Args:
            path: Path to checkpoint

        """
        fs = get_filesystem(path)
        if fs.exists(path):
            fs.rm(path, recursive=True)
            log.debug(f"Removed checkpoint: {path}")

    def _determine_dist_ckpt_save_strategy(self):
        """Determine the saving strategy based on constructor args.

        If self.async_save is True instantiates an async PyT Dist strategy,
        otherwise relies on MCore to create a proper strategy based on ckpt format.
        """
        save_strategy = (self.save_ckpt_format, 1)
        if self.async_save:
            if save_strategy[0] != 'torch_dist':
                raise ValueError('Async dist-ckpt save supported only for torch_dist format')
            save_strategy = TorchDistAsyncSaveShardedStrategy('torch_dist', 1)

        logging.info(f'Using {save_strategy} dist-ckpt save strategy.')
        return save_strategy


def _fix_tensors_device(ckpt: Dict) -> Dict:
    """Ensure checkpoint tensors are on the correct device."""
    assert torch.cuda.is_initialized(), (torch.cuda.is_available(), torch.cuda.is_initialized())
    cur_dev = torch.device("cuda", index=torch.cuda.current_device())
    from megatron.core.dist_checkpointing.dict_utils import dict_list_map_outplace

    def _fix_device(t):
        if isinstance(t, torch.Tensor) and t.is_cuda and t.device != cur_dev:
            t = t.to(cur_dev)
        return t

    return dict_list_map_outplace(_fix_device, ckpt)


def ckpt_to_dir(filepath: Union[str, Path]) -> Path:
    """PTL considers checkpoints as .ckpt files.
    This method removes the extension and returns a path
    to be used as a directory for distributed checkpoints.
    """
    filepath = Path(filepath)
    if not filepath.suffix == ".ckpt":
        filepath = filepath.with_suffix(filepath.suffix + ".ckpt")

    # adding this assert because we will later remove directories based on the return value of this method
    assert filepath.suffix == ".ckpt", f"filepath: {filepath} must have .ckpt extension"

    # create a new path whose name is the original filepath without the .ckpt extension
    checkpoint_dir = filepath.with_name(filepath.stem)

    return checkpoint_dir


def is_distributed_ckpt(path) -> bool:
    """Check if the given path corresponds to a distributed checkpoint directory.

    This function determines if the specified path is a directory that contains a distributed
    checkpoint by checking the directory's metadata.

    Args:
        path (Union[str, Path]): The path to check for being a distributed checkpoint.

    Returns
    -------
        bool: True if the path is a distributed checkpoint directory, False otherwise.

    """
    from megatron.core import dist_checkpointing

    checkpoint_dir = ckpt_to_dir(path)
    fs = get_filesystem(checkpoint_dir)
    if fs.isdir(checkpoint_dir) and dist_checkpointing.check_is_distributed_checkpoint(checkpoint_dir):
        return True

    return False
