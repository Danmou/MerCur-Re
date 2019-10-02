# main.py: Main entrypoint
#
# (C) 2019, Daniel Mouritzen

import datetime
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional, Tuple, Union

import click
import gin
import wandb
from loguru import logger
from tensorflow.python.util import deprecation

from project.logging import init_logging
from project.planet import run
from project.util import get_config_dir


@gin.configurable(whitelist=['logdir'])
def main(verbose: bool, logdir: Union[str, Path]) -> None:
    logdir = Path(logdir) / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    logdir.mkdir(parents=True)
    init_logging(verbose, logdir)
    wandb.config.update({name.rsplit('.', 1)[-1]: conf for (_, name), conf in gin.config._CONFIG.items()})
    with logger.catch():
        run(str(logdir))


@click.command()
@click.option('-c', '--config', type=click.Path(dir_okay=False), default=None, help='gin config')
@click.option('-l', '--logdir', type=click.Path(file_okay=False), default=None)
@click.option('--data', type=click.Path(file_okay=False), default=None,
              help="path to data directory (containing 'datasets' and 'scene_datasets')")
@click.option('-v', '--verbose', is_flag=True)
@click.option('-d', '--debug', is_flag=True, help='disable W&B syncing')
@click.option('--gpus', default=None)
@click.argument('extra_options', nargs=-1)
def main_command(config: str,
                 logdir: Optional[str],
                 data: Optional[str],
                 verbose: bool,
                 debug: bool,
                 gpus: Optional[str],
                 extra_options: Tuple[str, ...]
                 ) -> None:
    """
    Run training.

    EXTRA_OPTIONS is one or more additional gin-config options, e.g. 'planet.num_runs=1000'
    """
    deprecation._PRINT_DEPRECATION_WARNINGS = False
    if logdir:
        extra_options += (f'main.logdir="{logdir}"',)
    if debug:
        os.environ['WANDB_MODE'] = 'dryrun'
    if config is None:
        config = f'{get_config_dir()}/{"debug" if debug else "default"}.gin'
        if verbose:
            print(f'Using config {config}.')  # use print because logging has not yet been initialized
    if gpus is None and 'CUDA_VISIBLE_DEVICES' not in os.environ:
        print(f'Warning: No GPU devices specified. Defaulting to device 0.')
        os.environ['CUDA_VISIBLE_DEVICES'] = '0'
    if gpus is not None:
        os.environ['CUDA_VISIBLE_DEVICES'] = gpus
    wandb.init(project="thesis", sync_tensorboard=True)
    gin.parse_config_files_and_bindings([config], extra_options)
    with gin.unlock_config():
        gin.bind_parameter('main.logdir', str(Path(gin.query_parameter('main.logdir')).absolute()))
    tempdir = None
    try:
        if data:
            # Habitat assumes data is stored in local 'data' directory
            tempdir = TemporaryDirectory()
            (Path(tempdir.name) / 'data').symlink_to(Path(data).absolute())
            os.chdir(tempdir.name)
        main(verbose)
    finally:
        if tempdir:
            tempdir.cleanup()
