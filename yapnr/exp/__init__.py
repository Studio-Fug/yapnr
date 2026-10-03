"""Experiment campaigns on a local process pool, Google Cloud Batch or a Slurm allocation.

One backend-neutral task manifest (``yapnr-task-v1``) is planned from a campaign file and rendered
three ways: a niced local process pool, Batch job JSON for Spot VMs, and ``sbatch --array``
scripts that run the published image under Apptainer. The design is
docs/design/cloud-experiments.md; the owner's guide is docs/cloud-experiments.md.

Modules: ``spec`` (the two schemas), ``config`` (the owner configuration), ``bundle`` (content
addressed input archives), ``kinds`` (one module per kind of task), ``plan``, ``cost`` and
``prices`` (estimates and caps), ``store`` and ``cloud`` (storage and the ``gcloud`` boundary),
``backends`` (local, gcp-batch, slurm), ``task`` (the in-task wrapper, standard library only),
``fetch`` (results back into the local layout), ``calibration`` and ``cli``.
"""
