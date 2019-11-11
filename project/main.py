# main.py: Main entrypoint
#
# (C) 2019, Daniel Mouritzen

import contextlib
import datetime
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Generator, Optional, Tuple, Union

import gin
import wandb
import wandb.settings
import yaml
from loguru import logger
from tensorflow.python.util import deprecation

from project.execution import evaluate, train
from project.logging_utils import init_logging


@gin.configurable('main', whitelist=['base_logdir'])
class Main:
    def __init__(self,
                 verbosity: str,
                 base_logdir: Union[str, Path] = gin.REQUIRED,
                 debug: bool = False,
                 catch_exceptions: bool = True,
                 extension: Optional[str] = None,
                 ) -> None:
        deprecation._PRINT_DEPRECATION_WARNINGS = False
        self.debug = debug
        self.catch_exceptions = catch_exceptions
        self.base_logdir = Path(base_logdir)
        self.logdir = self._create_logdir(extension)
        init_logging(verbosity, self.logdir)
        if wandb.run.resumed:
            self._restore_wandb_checkpoint()
        else:
            self._create_symlinks()
            self._update_wandb()

    def _create_logdir(self, extension: Optional[str]) -> Path:
        logdir_name = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        if extension:
            logdir_name += f'-{extension}'
        logdir = self.base_logdir / logdir_name
        logdir.mkdir(parents=True)
        return logdir

    def _create_symlinks(self) -> None:
        latest_symlink = Path(self.base_logdir) / 'latest'
        if latest_symlink.exists():
            latest_symlink.unlink()
        latest_symlink.symlink_to(self.logdir)
        try:
            wandb_name = wandb.Api().run(wandb.run.path).name
        except wandb.apis.CommError:
            wandb_name = None
        if wandb_name:
            (Path(self.base_logdir) / wandb_name).symlink_to(self.logdir)
            logger.info(f'W&B run name: {wandb_name}')

    def _update_wandb(self) -> None:
        wandb.save(f'{self.logdir}/checkpoint')
        wandb.save(f'{self.logdir}/*.ckpt*')
        wandb.config.update({name.rsplit('.', 1)[-1]: conf
                             for (_, name), conf in gin.config._CONFIG.items()
                             if name is not None})
        wandb.config.update({'cuda_gpus': os.environ.get('CUDA_VISIBLE_DEVICES')})

    def _restore_wandb_checkpoint(self) -> None:
        restored_files = []
        try:
            ckpt_file = yaml.safe_load(wandb.restore('checkpoint')).get('model_checkpoint_path')
            restored_files.append('checkpoint')
            assert ckpt_file, "Can't resume wandb run: no checkpoint found!"
            ckpt_name = Path(ckpt_file).name
            for ext in ['index', 'meta', 'data-00000-of-00001']:
                name = f'{ckpt_name}.{ext}'
                restored_files.append(name)
                wandb.restore(name)
        finally:
            # Even if an error occurred, we don't want wandb to re-upload the downloaded files
            for file in restored_files:
                (Path(wandb.run.dir) / file).rename(self.logdir / file)

    @contextlib.contextmanager
    def _catch(self) -> Generator[None, None, None]:
        with logger.catch(BaseException, level='TRACE', reraise=not self.catch_exceptions):
            if self.catch_exceptions:
                with logger.catch(reraise=self.debug):
                    yield
            else:
                yield

    def train(self, initial_data: Optional[str] = None) -> None:
        try:
            with self._catch():
                train(str(self.logdir), initial_data)
        finally:
            # Make sure all checkpoints get uploaded
            wandb.save(f'{self.logdir}/checkpoint', policy='end')
            wandb.save(f'{self.logdir}/*.ckpt*', policy='end')

    def evaluate(self,
                 checkpoint: Optional[Path] = None,
                 num_episodes: int = 10,
                 video: bool = True,
                 seed: Optional[int] = None,
                 no_sync: bool = False,
                 ) -> None:
        if not checkpoint and wandb.run.resumed:
            checkpoint = self.logdir
        assert checkpoint is not None, 'No checkpoint specified!'
        with self._catch():
            evaluate(self.logdir,
                     checkpoint,
                     num_episodes,
                     video,
                     seed,
                     sync_wandb=wandb.run.resumed and not no_sync)


@contextlib.contextmanager
def main_configure(config: str,
                   extra_options: Tuple[str, ...],
                   verbosity: str,
                   debug: bool = False,
                   catch_exceptions: bool = True,
                   job_type: str = 'training',
                   data: Optional[str] = None,
                   extension: Optional[str] = None,
                   wandb_continue: Optional[str] = None,
                   ) -> Generator[Main, None, None]:
    if wandb_continue is not None:
        run = _get_wandb_run(wandb_continue)
        resume_args = dict(resume=True, id=run.id, name=run.name, config=run.config, notes=run.notes, tags=run.tags)
    else:
        resume_args = {}
    wandb.init(sync_tensorboard=True, job_type=job_type, **resume_args)
    gin.parse_config_files_and_bindings([config], extra_options)
    with gin.unlock_config():
        gin.bind_parameter('main.base_logdir', str(Path(gin.query_parameter('main.base_logdir')).absolute()))
    with open(Path(wandb.run.dir) / f'config_{job_type}.gin', 'w') as f:
        f.write(open(config).read())
        f.write('\n# Extra options\n')
        f.write('\n'.join(extra_options))
    tempdir = None
    try:
        if data:
            # Habitat assumes data is stored in local 'data' directory
            tempdir = TemporaryDirectory()
            (Path(tempdir.name) / 'data').symlink_to(Path(data).absolute())
            os.chdir(tempdir.name)
        yield Main(verbosity, debug=debug, catch_exceptions=catch_exceptions, extension=extension)
    finally:
        if tempdir:
            tempdir.cleanup()


def _get_wandb_run(name: str) -> wandb.apis.public.Run:
    api = wandb.Api()
    entity, project = (wandb.settings.Settings().get('default', setting) for setting in ['entity', 'project'])
    try:
        run = api.run(f'{entity}/{project}/{name}')
    except wandb.apis.CommError:
        # name is not a valid run id so we assume it's the name of a run
        runs = api.runs(f'{entity}/{project}', order="-created_at")
        for run in runs:
            if name in run.name:
                break
        else:
            raise ValueError(f'No run found with id or name {name}.')
    return run
