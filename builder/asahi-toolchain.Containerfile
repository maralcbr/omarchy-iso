ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG SOURCE_LOCK_SHA256
ARG TOOLCHAIN_PACKAGES

RUN pacman-key --init \
    && pacman --disable-sandbox --noconfirm -Sy archlinuxarm-keyring \
    && pacman-key --populate archlinuxarm \
    && pacman --disable-sandbox --noconfirm -Syu ${TOOLCHAIN_PACKAGES} \
    && pacman -Scc --noconfirm

RUN install -d -m 0755 /usr/share/omarchy-asahi-toolchain \
    && LC_ALL=C pacman -Q | LC_ALL=C sort \
      > /usr/share/omarchy-asahi-toolchain/packages.txt \
    && sha256sum /usr/share/omarchy-asahi-toolchain/packages.txt \
      > /usr/share/omarchy-asahi-toolchain/packages.sha256 \
    && printf '%s\n' "${SOURCE_LOCK_SHA256}" \
      > /usr/share/omarchy-asahi-toolchain/source-lock.sha256 \
    && chmod -R a-w /usr/share/omarchy-asahi-toolchain

LABEL org.omarchy.mx.asahi.toolchain="1"
LABEL org.omarchy.mx.asahi.source-lock-sha256="${SOURCE_LOCK_SHA256}"
