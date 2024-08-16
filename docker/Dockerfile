FROM ubuntu:latest
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies
RUN apt update -y \
    && apt install -qy build-essential wget libfontconfig1 git nodejs npm\
    && rm -rf /var/lib/apt/lists/*

# Install TexLive with scheme-basic
RUN wget http://mirror.ctan.org/systems/texlive/tlnet/install-tl-unx.tar.gz \
&& mkdir /install-tl-unx \
&& tar -xvf install-tl-unx.tar.gz -C /install-tl-unx --strip-components=1 \
&& echo "selected_scheme scheme-full" >> /install-tl-unx/texlive.profile \
&& /install-tl-unx/install-tl -profile /install-tl-unx/texlive.profile \
&& rm -r /install-tl-unx \
&& rm install-tl-unx.tar.gz

# Add TexLive to PATH
ENV PATH="/usr/local/texlive/2024/bin/x86_64-linux:${PATH}"


VOLUME ["/data"]


LABEL Maintainer="Biprateep Dey biprateep@pitt.edu"