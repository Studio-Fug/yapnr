"""The detailed router's A* kernels and the default one, in one place.

No imports: the regression runner (``regression/run.py``) reads the default here without
loading the engine. :func:`pnr.route.detail.maze.maze_kernel` selects the kernel a process
routes with (``PNR_MAZE_KERNEL``, else :data:`DEFAULT_MAZE_KERNEL`); ``run.py --maze-kernel``
defaults to the same constant and sets ``PNR_MAZE_KERNEL`` for every case; the ``ladder-cell``
kind of ``yapnr exp`` passes ``--maze-kernel`` only when a campaign names one
(``[config] maze_kernel``).

* ``native``: the packed search loop in C (:mod:`.native_maze`) when its library loads,
  else the packed kernel (the same routes, slower);
* ``packed``: the same search in Python over the dense field (:mod:`.packed_maze`);
* ``reference``: the dict/Cell search the other two are tested against.
"""

MAZE_KERNELS = ("native", "packed", "reference")
DEFAULT_MAZE_KERNEL = "native"
