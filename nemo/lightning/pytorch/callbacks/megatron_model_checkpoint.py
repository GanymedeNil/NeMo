# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import pytorch_lightning
import torch
from _weakref import proxy

from lightning_fabric.utilities.cloud_io import get_filesystem
from pytorch_lightning.callbacks.model_checkpoint import ModelCheckpoint as PTLModelCheckpoint, _is_local_file_protocol
from pytorch_lightning.utilities import rank_zero_info

from nemo.collections.common.callbacks import EMA
from nemo.utils import logging
from nemo.utils.app_state import AppState
from nemo.utils.callbacks.dist_ckpt_io import AsyncFinalizableCheckpointIO
from nemo.utils.get_rank import is_global_rank_zero
from nemo.utils.model_utils import ckpt_to_dir, inject_model_parallel_rank, uninject_model_parallel_rank
from nemo.utils.exp_manager import get_git_hash, get_git_diff
from nemo.utils.lightning_logger_patch import add_filehandlers_to_pl_logger
from shutil import copy, move

## TODO: I think everything related to dirs containing mp and tp ranks can be removed
class ModelCheckpoint(PTLModelCheckpoint):

    UNFINISHED_CHECKPOINT_SUFFIX = "-unfinished"

    def __init__(
        self,
        always_save_nemo: bool = False,
        save_nemo_on_train_end: bool = True,
        save_best_model: bool = False,
        postfix: str = ".nemo",
        n_resume: bool = False, ## TODO: what is this???
        #export: Optional[str] = None, ## used for exporting to something like hf.. do we want to include this in this class?
        # model_parallel_size: int = None, This will be passed by the MegatronStrategy
        async_save: bool = False,  # Should be an arg to the MegatronStrategy
        **kwargs,
    ):
        self.always_save_nemo = always_save_nemo
        self.save_nemo_on_train_end = save_nemo_on_train_end
        self.save_best_model = save_best_model
        if self.save_best_model and not self.save_nemo_on_train_end:
            logging.warning(
                (
                    "Found save_best_model is True and save_nemo_on_train_end is False. "
                    "Set save_nemo_on_train_end to True to automatically save the best model."
                )
            )
        self.postfix = postfix
        self.previous_best_path = ""
        #self.model_parallel_size = model_parallel_size
        self.async_save = async_save
        #self.export = export
        self.async_finalize_cb = None
        # Checkpoints which removal is deferred until async save is done.
        # Each element of `deferred_ckpts_to_remove` is a growing list
        # that `self._remove_checkpoint` adds to. Once `self._save_checkpoint`
        # is called, the last element is frozen and a new element is added.
        self.deferred_ckpts_to_remove: List[List[str]] = []

        # `prefix` is deprecated
        if 'prefix' in kwargs:
            self.prefix = kwargs.pop('prefix')
        else:
            self.prefix = ""

        # Call the parent class constructor with the remaining kwargs.
        super().__init__(**kwargs)

        if self.save_top_k != -1 and n_resume:
            logging.debug("Checking previous runs")
            self.nemo_topk_check_previous_run()
    
    ## TODO: make sure nothing is written to the log dir prior to this running
    def on_train_start(self, trainer, pl_module):
        if is_global_rank_zero():
            app_state = AppState()
            log_dir = app_state.log_dir

            # Check to see if any files exist that need to be moved
            files_to_move = []
            if Path(log_dir).exists():
                for child in Path(log_dir).iterdir():
                    if child.is_file():
                        files_to_move.append(child)

            if len(files_to_move) > 0:
                # Move old files to a new folder
                other_run_dirs = Path(log_dir).glob("run_*")
                run_count = 0
                for fold in other_run_dirs:
                    if fold.is_dir():
                        run_count += 1
                new_run_dir = Path(Path(log_dir) / f"run_{run_count}")
                new_run_dir.mkdir()
                for _file in files_to_move:
                    move(str(_file), str(new_run_dir))

            # Move files_to_copy to folder and add git information if present
            if AppState.files_to_copy:
                for _file in AppState.files_to_copy:
                    copy(Path(_file), log_dir)

            # Create files for cmd args and git info
            with open(log_dir / 'cmd-args.log', 'w', encoding='utf-8') as _file:
                _file.write(" ".join(AppState.cmd_args))

            # Try to get git hash
            git_repo, git_hash = get_git_hash()
            if git_repo:
                with open(log_dir / 'git-info.log', 'w', encoding='utf-8') as _file:
                    _file.write(f'commit hash: {git_hash}')
                    _file.write(get_git_diff())

            # Add err_file logging to global_rank zero
            logging.add_err_file_handler(log_dir / 'nemo_error_log.txt')

            # Add lightning file logging to global_rank zero
            add_filehandlers_to_pl_logger(log_dir / 'lightning_logs.txt', log_dir / 'nemo_error_log.txt')
        torch.distributed.barrier()

    ## I think this extracts the metrics from the checkpoint name
    ## and uses that to keep only best k
    ## we should figure out how to keep this functionality in nemo 2.0
    ## assuming we do, I don't think this code needs to be changed at all
    def nemo_topk_check_previous_run(self):
        try:
            self.best_k_models
            self.kth_best_model_path
            self.best_model_score
            self.best_model_path
        except AttributeError:
            raise AttributeError("Lightning's ModelCheckpoint was updated. NeMo's ModelCheckpoint will need an update.")
        self.best_k_models = {}
        self.kth_best_model_path = ""
        self.best_model_score = None
        self.best_model_path = ""

        checkpoints = list(path for path in self._saved_checkpoint_paths if not self._is_ema_filepath(path))
        for checkpoint in checkpoints:
            ## TODO: how does this work with nemo 2.0??
            ## figure out how to inject mp_rank into the filename!!
            if 'mp_rank' in str(checkpoint) or 'tp_rank' in str(checkpoint):
                checkpoint = uninject_model_parallel_rank(checkpoint)
            checkpoint = str(checkpoint)
            # second case is for distributed checkpoints, since they are a directory there's no extension
            if checkpoint[-10:] == '-last.ckpt' or checkpoint[-5:] == '-last':
                continue
            index = checkpoint.find(self.monitor) + len(self.monitor) + 1  # Find monitor in str + 1 for '='
            if index != len(self.monitor):
                match = re.search('[A-z]', checkpoint[index:])
                if match:
                    value = checkpoint[index : index + match.start() - 1]  # -1 due to separator hypen
                    self.best_k_models[checkpoint] = float(value)
        if len(self.best_k_models) < 1:
            return  # No saved checkpoints yet

        _reverse = False if self.mode == "min" else True

        best_k_models = sorted(self.best_k_models, key=self.best_k_models.get, reverse=_reverse)

        # This section should be ok as rank zero will delete all excess checkpoints, since all other ranks are
        # instantiated after rank zero. models_to_delete should be 0 for all other ranks.
        if self.model_parallel_size is not None:
            # check for distributed checkpoint
            if checkpoints[0].is_dir():
                models_to_delete = len(best_k_models) - self.save_top_k
            else:
                models_to_delete = len(best_k_models) - self.model_parallel_size * self.save_top_k
        else:
            models_to_delete = len(best_k_models) - self.save_top_k

        models_to_delete = max(0, models_to_delete)
        logging.debug(f'Number of models to delete: {models_to_delete}')

        # If EMA enabled, delete the additional EMA weights
        ema_enabled = self._has_ema_ckpts(self._saved_checkpoint_paths)

        for _ in range(models_to_delete):
            model = best_k_models.pop(-1)
            self.best_k_models.pop(model)
            self._del_model_without_trainer(model)
            if ema_enabled and self._fs.exists(self._ema_format_filepath(model)):
                self._del_model_without_trainer(self._ema_format_filepath(model))
            logging.debug(f"Removed checkpoint: {model}")

        self.kth_best_model_path = best_k_models[-1]
        self.best_model_path = best_k_models[0]
        self.best_model_score = self.best_k_models[self.best_model_path]

    def _remove_invalid_entries_from_topk(self):
        # Removes invalid (incomplete or not existing) checkpoints from topk checkpoints.
        # This might be needed if the checkpointing was abruptly terminated.
        def __is_ckpt_ok(ckpt_path: str) -> bool:
            exists = (
                os.path.isfile(ckpt_path)
                or os.path.isfile(inject_model_parallel_rank(ckpt_path))
                or os.path.isdir(ckpt_path.removesuffix('.ckpt'))
            )
            return exists and not self.is_checkpoint_unfinished(ckpt_path)

        self.best_k_models = {k: v for k, v in self.best_k_models.items() if __is_ckpt_ok(k)}
        if len(self.best_k_models) > 0:
            reverse_arr = self.mode != "min"
            best_k_models_arr = sorted(self.best_k_models, key=self.best_k_models.get, reverse=reverse_arr)
            self.kth_best_model_path = best_k_models_arr[-1]
            self.kth_value = self.best_k_models[self.kth_best_model_path]
            self.best_model_path = best_k_models_arr[0]
            self.best_model_score = self.best_k_models[self.best_model_path]
        else:
            self.kth_best_model_path = ""
            self.kth_value = None
            self.best_model_path = ""
            self.best_model_score = None

    def load_state_dict(self, state_dict: Dict[str, Any]) -> None:
        super().load_state_dict(state_dict)
        self._remove_invalid_entries_from_topk()

    def setup(self, *args, **kwargs) -> None:
        if is_global_rank_zero():
            logging.debug("Removing unfinished checkpoints if any...")
            ModelCheckpoint._remove_unfinished_checkpoints(self.dirpath)
        # Ensure that all ranks continue with unfinished checkpoints removed
        if torch.distributed.is_initialized():
            torch.distributed.barrier()
        super().setup(*args, **kwargs)

    ## TODO: this has to be updated for new checkpointing
    ### old implementation saved .nemo file but we don't have that anymore
    def on_save_checkpoint(self, trainer, pl_module, checkpoint):
        output = super().on_save_checkpoint(trainer, pl_module, checkpoint)
        '''if self.export and is_global_rank_zero:
            ## TODO: optionally tar as .nemo
            ## TODO: figure out how this works!!!
            ## not sure of the export() implementation right now
            io.export_ckpt(export_dir, self.export)
        if torch.distributed.is_initialized():
            torch.distributed.barrier()'''
        return output

    def on_train_end(self, trainer, pl_module):
        ## TODO: add support! 
        '''if trainer.fast_dev_run:
            return None'''

        # check if we need to save a last checkpoint manually as validation isn't always run based on the interval
        if self.save_last and trainer.val_check_interval != 0:
            should_save_last_checkpoint = False
            if isinstance(trainer.val_check_interval, float) and trainer.val_check_interval % trainer.global_step != 0:
                should_save_last_checkpoint = True
            if isinstance(trainer.val_check_interval, int) and trainer.global_step % trainer.val_check_interval != 0:
                should_save_last_checkpoint = True
            if should_save_last_checkpoint:
                monitor_candidates = self._monitor_candidates(trainer)
                if self.last_model_path == self.format_checkpoint_name(monitor_candidates, self.CHECKPOINT_NAME_LAST):
                    logging.debug(f'Last checkpoint {self.last_model_path} already saved')
                else:
                    super()._save_last_checkpoint(trainer, monitor_candidates)
        # Call parent on_train_end() to save the -last checkpoint
        super().on_train_end(trainer, pl_module)

        # Load the best model and then re-save it
        if self.save_best_model:
            # wait for all processes
            trainer.strategy.barrier("SaveBestCheckpointConnector.resume_end")
            if self.best_model_path == "":
                logging.warning(
                    f"{self} was told to save the best checkpoint at the end of training, but no saved checkpoints "
                    "were found. Saving latest model instead."
                )
            
            ## TODO: figure out if this needs to be changed
            ## I think our checkpoints don't end in ".ckpt"
            else:
                ## TODO: do we need this if statement?
                if os.path.isdir(self.best_model_path.split('.ckpt')[0]):
                    self.best_model_path = self.best_model_path.split('.ckpt')[0]
                self.best_model_path = trainer.strategy.broadcast(self.best_model_path)
                ## TODO: understand how this saves the model checkpoint
                trainer._checkpoint_connector.restore(self.best_model_path)

        '''if self.export and is_global_rank_zero:
            ## TODO: optionally tar as .nemo
            ## also figure out what export_dir and export are supposed to be
            io.export_ckpt(export_dir, export)
        if torch.distributed.is_initialized():
            torch.distributed.barrier()'''
            
        '''if self.save_nemo_on_train_end:
            backup_path = self._backup_existing_nemo_ckpt(trainer)
            pl_module.save_to(save_path=self._format_nemo_checkpoint_name())
            if backup_path is not None and is_global_rank_zero():
                logging.info(f'Removing old .nemo backup {backup_path}')
                get_filesystem(backup_path).rm(backup_path)'''

    ## TODO: update this for nemo 2.0
    def _backup_existing_nemo_ckpt(self, trainer) -> Optional[str]:
        """Search for an available name with version infix and rename existing checkpoint.

        NOTE: this behavior is slightly different from regular checkpoints.
        PTL creates new regular checkpoint with the first available name.
        Here, for backward compatibility, we create .nemo checkpoint as before
        and create a backup under the first available name.

        Args:
            trainer (Trainer): trainer instance.

        Returns:
            Path to the backup checkpoint or None, if no backup was created
        """
        base_path = self._format_nemo_checkpoint_name()
        available_path = base_path
        if self._enable_version_counter:
            version_cnt = self.STARTING_VERSION
            while self.file_exists(available_path, trainer, check_dist_ckpt=False):
                available_path = self._format_nemo_checkpoint_name(version_cnt)
                version_cnt += 1
        if available_path == base_path:
            # no existing ckpt, no need to backup
            return None
        if trainer.is_global_zero:
            logging.info(f'{base_path} already exists, moving existing checkpoint to {available_path}')
            shutil.move(base_path, available_path)
        trainer.strategy.barrier()
        return available_path

    def _format_nemo_checkpoint_name(self, ver: Optional[int] = None) -> str:
        version_infix = '' if ver is None else f'{self.CHECKPOINT_JOIN_CHAR}v{ver}'
        return os.path.abspath(
            os.path.expanduser(os.path.join(self.dirpath, self.prefix + version_infix + self.postfix))
        )

    def _del_model_without_trainer(self, filepath: str) -> None:

        filepath = Path(filepath)

        # check if filepath is a distributed a checkpoint
        if ckpt_to_dir(filepath).is_dir():
            if is_global_rank_zero():
                try:
                    dist_ckpt = ckpt_to_dir(filepath)
                    shutil.rmtree(dist_ckpt, ignore_errors=True)
                    logging.info(f"Removed distributed checkpoint: {dist_ckpt}")
                except:
                    logging.info(f"Tried to remove distributed checkpoint: {dist_ckpt} but failed.")

        else:
            app_state = AppState()

            # legacy model parallel checkpoint
            if app_state.model_parallel_size is not None and app_state.model_parallel_size > 1:
                # filepath needs to be updated to include mp_rank
                filepath = inject_model_parallel_rank(filepath)

            # each model parallel rank needs to remove its model
            if is_global_rank_zero() or (
                app_state.model_parallel_size is not None and app_state.data_parallel_rank == 0
            ):
                try:
                    self._fs.rm(filepath)
                    logging.info(f"Removed checkpoint: {filepath}")
                except:
                    logging.info(f"Tried to remove checkpoint: {filepath} but failed.")

    def _ema_callback(self, trainer: 'pytorch_lightning.Trainer') -> Optional[EMA]:
        ema_callback = None
        for callback in trainer.callbacks:
            if isinstance(callback, EMA):
                ema_callback = callback
        return ema_callback

    @staticmethod
    def format_checkpoint_unfinished_marker_path(checkpoint_path: Union[Path, str]) -> Path:
        """Format the path to the unfinished checkpoint marker file.

        If the marker file exists, corresponding checkpoint is considered unfinished/incomplete.
        NOTE: Marker path for the EMA checkpoint part is the same as for the original checkpoint.

        Args:
            checkpoint_path: Path to the checkpoint file or dir.
              Does not need to exist.

        Returns:
            Path to the unfinished checkpoint marker file.
        """
        marker_filepath = str(uninject_model_parallel_rank(checkpoint_path))
        marker_filepath = marker_filepath.removesuffix(".nemo")
        marker_filepath = marker_filepath.removesuffix(".ckpt")
        marker_filepath = marker_filepath.removesuffix("-EMA")
        return Path(marker_filepath + ModelCheckpoint.UNFINISHED_CHECKPOINT_SUFFIX)

    @staticmethod
    def is_checkpoint_unfinished(checkpoint_path: Union[Path, str]) -> bool:
        """Check if the checkpoint is unfinished.

        Args:
            checkpoint_path: Path to the checkpoint file or dir.
              Does not need to exist.

        Returns:
            True if the checkpoint is unfinished, False otherwise.
        """
        return ModelCheckpoint.format_checkpoint_unfinished_marker_path(checkpoint_path).exists()

    @staticmethod
    def set_checkpoint_unfinished_marker(checkpoint_path: Union[Path, str], barrier_after=False) -> None:
        """Marks given checkpoint as unfinished.

        Args:
            checkpoint_filepath: Path to the checkpoint file or dir.
              Does not need to exist.
            barrier_after: Synchronize ranks after writing the marker file.
              Defaults to False.
        """
        if is_global_rank_zero():
            marker_path = ModelCheckpoint.format_checkpoint_unfinished_marker_path(checkpoint_path)
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.touch()
        if barrier_after and torch.distributed.is_initialized():
            torch.distributed.barrier()

    @staticmethod
    def remove_checkpoint_unfinished_marker(checkpoint_path: Union[Path, str], barrier_before=False) -> None:
        """Clear unfinished marker for given checkpoint.

        Args:
            checkpoint_path: Path to the checkpoint file or dir.
              Does not need to exist.
            barrier_before: Synchronize ranks before removing the marker file.
              Defaults to False.
        """
        try:
            if barrier_before and torch.distributed.is_initialized():
                torch.distributed.barrier()
            if is_global_rank_zero():
                marker_path = ModelCheckpoint.format_checkpoint_unfinished_marker_path(checkpoint_path)
                if marker_path.exists():
                    marker_path.unlink()
        except:
            return

    def file_exists(self, filepath: str, trainer: "pytorch_lightning.Trainer", check_dist_ckpt: bool = True) -> bool:
        """Checks if a file or a file without a suffix (distributed checkpoint) exists."""
        exists = self._fs.exists(filepath) or (check_dist_ckpt and self._fs.exists(ckpt_to_dir(filepath)))
        return trainer.strategy.broadcast(exists)


    def _save_checkpoint(self, trainer: 'pytorch_lightning.Trainer', filepath: str) -> None:
        # barrier_after=True, so all ranks continue after the unfinished checkpoint marker is placed.
        # if anything goes wrong during checkpointing, we should be able to detect that data is incomplete.
        self.set_checkpoint_unfinished_marker(filepath, barrier_after=True)
        ema_callback = self._ema_callback(trainer)
        if ema_callback is not None:
            if self.async_save:
                raise ValueError('async_save with EMA not supported')
            with ema_callback.save_original_optimizer_state(trainer):
                super()._save_checkpoint(trainer, filepath)

            # save EMA copy of the model as well.
            with ema_callback.save_ema_model(trainer):
                filepath = self._ema_format_filepath(filepath)
                if self.verbose:
                    rank_zero_info(f"Saving EMA weights to separate checkpoint {filepath}")
                super()._save_checkpoint(trainer, filepath)
            self.remove_checkpoint_unfinished_marker(filepath, barrier_before=True)
        else:
            # Async save passed the finalization function to checkpoint_io,
            # sync save calls the finalization function immediately after save.
            finalize_fn = self._get_finalize_save_checkpoint_callback(trainer, filepath, trainer.global_step)
            if self.async_save:
                checkpoint_io = trainer.strategy.checkpoint_io
                if not isinstance(checkpoint_io, AsyncFinalizableCheckpointIO):
                    raise ValueError('Async save requires async compatible CheckpointIO')
                storage_options = dict(finalize_fn=finalize_fn)
                # Each upcoming ckpt removal request will be executed as part of this save finalization
                self.deferred_ckpts_to_remove.append([])
            else:
                storage_options = None
            trainer.save_checkpoint(filepath, self.save_weights_only, storage_options=storage_options)
            if self.async_save:
                logging.info(f'Scheduled async checkpoint save for {filepath}')
            else:
                finalize_fn()

    def _get_finalize_save_checkpoint_callback(
        self, trainer: 'pytorch_lightning.Trainer', filepath: str, global_step: int
    ):
        """Creates a callback that can be used to finalize async (and sync) ckpt saves."""

        def _cb():
            logging.debug(f'Finalize callback called for step {global_step}, filepath {filepath}')
            self._last_global_step_saved = global_step
            self._last_checkpoint_saved = filepath

            # notify loggers
            if trainer.is_global_zero:
                for logger in trainer.loggers:
                    logger.after_save_checkpoint(proxy(self))

            # barrier_before=True, so all ranks synchronize before removing the unfinished checkpoint marker
            # we don't want to remove the marker until all checkpointing is done.
            self.remove_checkpoint_unfinished_marker(filepath, barrier_before=True)

            if not self.async_save:
                return

            logging.info(f'Async checkpoint save for step {global_step} ({filepath}) finalized successfully.')

            # Remove checkpoints marked for removal by `self._remove_checkpoint`
            # For each finalization there is exactly one entry in self.deferred_ckpts_to_remove
            assert self.deferred_ckpts_to_remove
            ckpts_to_remove = self.deferred_ckpts_to_remove.pop(0)
            logging.debug(f'Checkpoints to remove: {ckpts_to_remove}')
            for ckpt_to_remove in ckpts_to_remove:
                self._remove_checkpoint(trainer, ckpt_to_remove, override_async=True)

        return _cb

    def _remove_checkpoint(self, trainer: "pytorch_lightning.Trainer", filepath: str, override_async=False) -> None:
        """Performs checkpoint removal or deferred removal.

        With async save, `self._remove_checkpoint` is called before the checkpoint
        is actually finished so we can't remove it. Instead we add it to
        `self.deferred_ckpts_to_remove` for future removal.
        """
        if self.async_save and not override_async:
            # Register checkpoint removal in the last (active) checkpoint removal list
            self.deferred_ckpts_to_remove[-1].append(filepath)
            return
        # barrier_after=True, so all ranks continue after the unfinished checkpoint marker is placed.
        # if anything goes wrong during removal, we should be able to detect that data is incomplete.
        self.set_checkpoint_unfinished_marker(filepath, barrier_after=True)
        super()._remove_checkpoint(trainer, filepath)
        ema_callback = self._ema_callback(trainer)
        if ema_callback is not None:
            # remove EMA copy of the state dict as well.
            filepath = self._ema_format_filepath(filepath)
            super()._remove_checkpoint(trainer, filepath)
        # barrier_before=True, so all ranks synchronize before removing the unfinished checkpoint marker
        # we don't want to remove the marker until the checkpoint is actually removed.
        self.remove_checkpoint_unfinished_marker(filepath, barrier_before=True)

    def _ema_format_filepath(self, filepath: str) -> str:
        return filepath.replace(self.FILE_EXTENSION, f'-EMA{self.FILE_EXTENSION}')

    def _has_ema_ckpts(self, checkpoints: Iterable[Path]) -> bool:
        return any(self._is_ema_filepath(checkpoint_path) for checkpoint_path in checkpoints)

    def _is_ema_filepath(self, filepath: Union[Path, str]) -> bool:
        return str(filepath).endswith(f'-EMA{self.FILE_EXTENSION}')

    @property
    def _saved_checkpoint_paths(self) -> Iterable[Path]:
        # distributed checkpoints are directories so we check for them here
        # we filter out unfinished checkpoints, these should be deleted during next cleanup
        dist_checkpoints = [d for d in Path(self.dirpath).glob("*") if d.is_dir()]
        if dist_checkpoints:
            return filter(lambda p: not self.is_checkpoint_unfinished(p), dist_checkpoints)
        else:
            checkpoint_files = [f for f in Path(self.dirpath).rglob("*.ckpt")]
            return filter(lambda p: not self.is_checkpoint_unfinished(p), checkpoint_files)

    @staticmethod
    def _remove_unfinished_checkpoints(checkpoint_dir: Union[Path, str]) -> None:

        # Delete unfinished checkpoints from the filesystems.
        # "Unfinished marker" files are removed as well.

        if not is_global_rank_zero():
            raise AssertionError("_remove_unfinished_checkpoints should run only on rank 0")

        print(f'checkpoint dir: {checkpoint_dir}')
        checkpoint_dir = Path(checkpoint_dir)

        existing_marker_filepaths = {
            f.resolve()
            for f in checkpoint_dir.glob(f"*{ModelCheckpoint.UNFINISHED_CHECKPOINT_SUFFIX}")
            if f.is_file()
        }

        ## TODO: figure out .ckpt suffix -- default in ptl?
        checkpoint_filepaths = {f.resolve() for f in checkpoint_dir.rglob("*.ckpt")}
        for ckpt_filepath in checkpoint_filepaths:
            possible_marker_path = ModelCheckpoint.format_checkpoint_unfinished_marker_path(ckpt_filepath)
            if possible_marker_path in existing_marker_filepaths:
                logging.warning(f'Removing unfinished checkpoint: {ckpt_filepath}')
                os.remove(ckpt_filepath)

        # some directories might be distributed checkpoints, we remove these if they have a unfinished marker
        all_dirpaths = {d.resolve() for d in checkpoint_dir.glob("*") if d.is_dir()}
        for ckpt_dirpath in all_dirpaths:
            possible_marker_path = ModelCheckpoint.format_checkpoint_unfinished_marker_path(ckpt_dirpath)
            if possible_marker_path in existing_marker_filepaths:
                logging.warning(f'Removing unfinished dist checkpoint: {ckpt_dirpath}')
                shutil.rmtree(ckpt_dirpath)

        # delete markers
        for marker_path in existing_marker_filepaths:
            os.remove(marker_path)

    def _should_remove_checkpoint(self, trainer: "pl.Trainer", previous: str, current: str) -> bool:
        """Checks if the previous checkpoint should be deleted.
        A checkpoint won't be deleted if any of the cases apply:
        - The previous checkpoint is the same as the current checkpoint (means the old was already overwritten by new)
        - The previous checkpoint is not in the current checkpoint directory and the filesystem is local
        - The previous checkpoint is the checkpoint the Trainer resumed from and the filesystem is local
            and the resumed from checkpoint is not the last checkpoint
        """
        if previous == current:
            return False
        if not _is_local_file_protocol(previous):
            return True
        previous = Path(previous).absolute()
        resume_path = Path(trainer.ckpt_path).absolute() if trainer.ckpt_path is not None else None

        if resume_path is not None and previous == resume_path:
            if str(current).endswith("-last.ckpt") and resume_path.name.endswith("-last.ckpt"):
                # delete the previous `-last.ckpt` checkpoint when current saved checkpoint is also `-last.ckpt`, if they're in the same directory
                pass
            else:
                return False
        if self.dirpath is None:
            raise ValueError(f"{self.__class__}.dirpath is None.")
        dirpath = Path(self.dirpath).absolute()
        return dirpath in previous.parents