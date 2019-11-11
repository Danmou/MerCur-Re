# statistics.py: Provides Statistics utility class
#
# (C) 2019, Daniel Mouritzen

from pathlib import Path
from typing import Any, Dict, Sequence, Union

import numpy as np

from .pretty_printer import PrettyPrinter


class Statistics:
    """Calculate mean, variance and standard deviation from streaming data represented as dicts"""
    def __init__(self, keys: Sequence[str], save_file: Union[str, Path, None] = None) -> None:
        self._keys = keys
        self._count = 0
        self._means = np.zeros([len(keys)])
        self._mean_squares = np.zeros([len(keys)])
        self._file = save_file and open(save_file, 'w')
        if self._file:
            self._file.write(','.join(self._keys) + '\n')

    def _from_dict(self, data: Dict[str, float]) -> np.ndarray:
        return np.array([data[k] for k in self._keys])

    def _to_dict(self, data: np.ndarray) -> Dict[str, float]:
        return dict(zip(self._keys, data))

    def update(self, data_dict: Dict[str, float]) -> None:
        data = self._from_dict(data_dict)
        if self._file:
            self._file.write(','.join(str(d) for d in data) + '\n')
        keep_ratio = self._count / (self._count + 1)
        self._means = self._means * keep_ratio + data * (1 - keep_ratio)
        self._mean_squares = self._mean_squares * keep_ratio + data ** 2 * (1 - keep_ratio)
        self._count += 1

    @property
    def mean(self) -> Dict[str, float]:
        return self._to_dict(self._means)

    def _get_variance(self) -> np.ndarray:
        return self._mean_squares - self._means**2

    @property
    def variance(self) -> Dict[str, float]:
        return self._to_dict(self._get_variance())

    @property
    def stddev(self) -> Dict[str, float]:
        return self._to_dict(np.sqrt(self._get_variance()))

    def print(self, **kwargs: Any) -> None:
        """Pretty print statistics. Accepts same keyword args as PrettyPrinter's initializer"""
        pp = PrettyPrinter(['_'] + list(self._keys), **kwargs)
        pp.print_header()
        pp.print_row(dict(_='mean', **self.mean))
        pp.print_row(dict(_='var', **self.variance))
        pp.print_row(dict(_='stddev', **self.stddev))
