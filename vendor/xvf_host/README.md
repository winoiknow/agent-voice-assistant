# vendor/xvf_host

The reSpeaker XVF3800 control binary (`xvf_host`) is **not committed** here. The
installer (`install.sh`, Phase 8) fetches the correct prebuilt binary for the host
architecture from the upstream `host_control/` directory:

<https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/tree/master/host_control>

Prebuilt targets include Raspberry Pi 64-bit, Linux x86_64, Jetson, macOS ARM64, and
Windows. Place the binary on `PATH` (or point `respeaker.xvf_host_path` at it).

For development without hardware, set `respeaker.simulate: true` and the in-memory
`MockXvfHost` is used instead — no binary required.

This directory is git-ignored (see `/.gitignore`) so vendored binaries never land
in the repo.
