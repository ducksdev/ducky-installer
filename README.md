# Ducky Pool ckpool

The BCH solo mining pool engine packaged for the [Ducky Pool installer](https://github.com/ducksdev/ducky-installer).

## What this image is

A thin wrapper around the upstream `wim-solo-ckpool` Docker image, re-tagged under the `ducksdev` namespace so the Ducky Pool installer can reference a stable URL we control. The runtime binaries are the upstream binaries — we don't recompile or modify them.

## What this image is NOT

- A from-scratch ckpool reimplementation. We rely on years of community work that came before us.
- A different software product. Functionally it's the same pool engine the upstream image provides.
- Compatible with anything other than Bitcoin Cash. ckpool was originally BTC-only; the BCH support comes from the upstream chain documented below.

## Lineage

```
Con Kolivas (ckpool, BTC-only, GPLv3)
   └─> AxeBCH (added BCH support: CashAddr, BCH coinbase format)
       └─> WillItMod (wim-solo-ckpool: solo-mining refinements + Docker packaging)
           └─> ducksdev (this image: thin re-tag for the Ducky Pool installer)
```

Each step in this chain is a credit-worthy piece of work. Without the upstream, there's no Ducky Pool.

## License

GPLv3, inherited from upstream. This is not a choice — GPLv3 propagates to derivative works. If you fork this image or its packaging, your fork must also be GPLv3.

See [`COPYING`](https://github.com/willitmod/wim-solo-ckpool) in the upstream repo for the full license text.

## How to use

This image is designed to be pulled by the Ducky Pool installer:

```bash
docker pull ghcr.io/ducksdev/ducky-pool-ckpool:0.8.3-rc1-590fb2a
```

You can also run it standalone the same way you'd run the upstream image — same arguments, same config format, same socket layout. See [the upstream README](https://github.com/willitmod/wim-solo-ckpool) for runtime details.

## How this image is built

The [`Dockerfile`](./Dockerfile) is a few lines: it pulls the upstream image, adds OCI labels (title, source, license, lineage), and pushes the result to GHCR. The CI workflow at [`.github/workflows/publish-ckpool.yml`](../.github/workflows/publish-ckpool.yml) handles the build.

To publish a new version:

1. Update the upstream pin in [`Dockerfile`](./Dockerfile) (the `FROM` line)
2. Push a git tag matching the upstream version: `git tag ckpool-v0.9.0-... && git push --tags`
3. The CI workflow runs and publishes the new image to `ghcr.io/ducksdev/ducky-pool-ckpool:VERSION` and `:latest`

## Reporting issues

Bugs in the actual ckpool engine should go to [WillItMod's issue tracker](https://github.com/willitmod/wim-solo-ckpool/issues). Bugs in the Ducky Pool installer or dashboard go to [ducksdev/ducky-installer](https://github.com/ducksdev/ducky-installer/issues). Bugs specifically in how this image is packaged (labels, build pipeline) go here.

## Contact

ducksdev — see the [main installer repo](https://github.com/ducksdev/ducky-installer) for contact info.
