# openEMS (GPL-3.0) for the radar60 RF sign-off, run as an external process.
#
# Ubuntu 24.04 no longer packages openEMS (`apt-cache showpkg openems` lists no version on noble),
# so this is a source build of openEMS-Project at a pinned tag, CLI and Python bindings, no GUI.
# The same file builds natively for linux/arm64 (a Mac running Docker) and linux/amd64 (a cloud
# image); Batch VMs have no internet, so a cloud run needs this image pushed to the project's
# registry first (see ../README.md, "EM sign-off").
#
#   docker build -t radar60-openems:0.37.0-rc3 examples/radar60/rf/openems
#   docker run --rm --cpus 4 -v "$PWD":/w -w /w radar60-openems:0.37.0-rc3 \
#     python3 examples/radar60/rf/openems/column_sim.py --out out/col --threads 4
FROM ubuntu:24.04

ARG OPENEMS_REF=v0.37.0-rc3
ARG NJOBS=3
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git sudo build-essential cmake \
      python3 python3-dev python3-venv python3-pip python3-numpy python3-h5py python3-matplotlib \
      cython3 python3-setuptools python3-setuptools-scm \
      libhdf5-dev libtinyxml-dev libboost-all-dev libcgal-dev libvtk9-dev \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --recursive --depth 1 --branch "${OPENEMS_REF}" \
      https://github.com/thliebig/openEMS-Project.git /src/openEMS-Project \
 && cd /src/openEMS-Project \
 && git rev-parse HEAD > /opt/openems-project.commit \
 && ./scripts/install_deps.sh --check --disable-gui --python

RUN cd /src/openEMS-Project \
 && ./update_openEMS.sh /opt/openEMS --python --disable-GUI --njobs="${NJOBS}" \
      --python-venv-mode site --python-venv-dir /opt/openEMS/venv \
 && /opt/openEMS/venv/bin/python -c "import CSXCAD, openEMS; print('openEMS python ok')"

ENV PATH=/opt/openEMS/venv/bin:/opt/openEMS/bin:$PATH \
    LD_LIBRARY_PATH=/opt/openEMS/lib \
    OMP_NUM_THREADS=1
