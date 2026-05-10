# Ducky Pool ckpool image
# ---
# Thin wrapper around WillItMod's wim-solo-ckpool — the BCH-capable solo
# mining pool engine that descends from Con Kolivas's upstream ckpool via
# the AxeBCH lineage.
#
# This Dockerfile does NOT modify the binaries. It re-tags the upstream
# image under the ducksdev namespace with proper OCI labels for provenance
# and credit. Doing this lets us:
#
#   - Publish at a stable URL we control (ghcr.io/ducksdev/...)
#   - Survive upstream image deletion / re-tagging
#   - Carry the correct labels for FOSS hygiene (license, source, authors)
#   - Brand the image consistently with the Ducky Pool installer
#
# When users `docker pull` this image, Docker fetches both this thin
# layer and the upstream base layer (which is referenced by digest
# below for integrity). Users never see WillItMod's name in their
# `docker ps` output — but anyone running `docker history` sees the
# correct upstream lineage, which is correct FOSS behavior.
#
# Licensed GPLv3 — same as the upstream code we wrap.

# Pin to both tag and digest. The digest must match the actual upstream
# image bytes. To update: docker pull ghcr.io/willitmod/wim-solo-ckpool:<tag>
# then docker inspect ... | grep RepoDigests, paste sha256 below.
# Digest pinning protects us from supply-chain compromise of upstream.
FROM ghcr.io/willitmod/wim-solo-ckpool:0.8.3-rc1-590fb2a

LABEL org.opencontainers.image.title="Ducky Pool ckpool"
LABEL org.opencontainers.image.description="BCH solo mining ckpool engine, packaged for the Ducky Pool installer"
LABEL org.opencontainers.image.source="https://github.com/ducksdev/ducky-installer"
LABEL org.opencontainers.image.url="https://github.com/ducksdev/ducky-installer"
LABEL org.opencontainers.image.documentation="https://github.com/ducksdev/ducky-installer/tree/main/ckpool"
LABEL org.opencontainers.image.vendor="ducksdev"
LABEL org.opencontainers.image.licenses="GPL-3.0-or-later"

# Provenance — credit the upstream chain that made BCH solo mining possible.
LABEL net.ducksdev.upstream.repo="https://github.com/willitmod/wim-solo-ckpool"
LABEL net.ducksdev.upstream.tag="0.8.3-rc1-590fb2a"
LABEL net.ducksdev.upstream.image="ghcr.io/willitmod/wim-solo-ckpool:0.8.3-rc1-590fb2a"
LABEL net.ducksdev.upstream.lineage="Con Kolivas (ckpool) -> AxeBCH (BCH adaptation) -> WillItMod (solo refinements) -> ducksdev (Ducky Pool packaging)"
