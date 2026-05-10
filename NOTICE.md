# Notice

This image (`ghcr.io/ducksdev/ducky-pool-ckpool`) is a packaging of upstream work by other authors. We claim no original code authorship — our contribution is the OCI labels, the build pipeline, and the choice to make this available under the `ducksdev` namespace for the [Ducky Pool installer](https://github.com/ducksdev/ducky-installer).

## Upstream chain

The binaries in this image were compiled by **WillItMod** from the source at:

> https://github.com/willitmod/wim-solo-ckpool

WillItMod's fork derives from earlier work:

- **AxeBCH** — added Bitcoin Cash support (CashAddr address parsing, BCH-specific coinbase transaction format, BCH consensus rule handling) to ckpool. Without this work, ckpool would still be BTC-only and Ducky Pool would not exist.

- **Con Kolivas and Andrew Smith** — original authors of ckpool, ckdb, and libckpool. Source repo: https://bitbucket.org/ckolivas/ckpool. Years of low-level C work that the entire community builds on.

## License

Everything in this lineage is **GPLv3**. This image inherits that license. You can:

- Pull and run this image freely
- Use it commercially
- Modify it (subject to publishing your modifications under GPLv3)
- Redistribute it (subject to maintaining the upstream credits)

You cannot:

- Relicense this image or its underlying binaries to anything other than GPLv3
- Strip the upstream credits in this NOTICE
- Strip the OCI labels in the image that point to upstream sources

See `COPYING` in any upstream repo for the full GPLv3 license text.

## Trademarks

"ckpool" is the project name used by Con Kolivas. "Ducky Pool" is our project name. We don't claim "ckpool" — we credit it. If anyone from the upstream chain has trademark concerns about how we present this image, please open an issue on https://github.com/ducksdev/ducky-installer and we'll address it immediately.

## What ducksdev contributes

- This `Dockerfile` (a few lines)
- This NOTICE
- The CI workflow that publishes the image to GHCR
- The Ducky Pool installer that pulls and configures this image
- The Ducky Pool dashboard that reads ckpool's output and presents it nicely

None of these are derivative works of the underlying C source — they sit at arm's length and communicate via files and IPC sockets. The dashboard is independently licensed (see the main installer repo).
