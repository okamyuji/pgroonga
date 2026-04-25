#!/bin/sh
set -eux

PGROONGA_VERSION=$1
GROONGA_VERSION=$2
MECAB_VERSION=$3
MECAB_IPADIC_VERSION=$4

# Build and install MeCab
cd /tmp
wget "https://github.com/shogo82148/mecab/releases/download/v${MECAB_VERSION}/mecab-${MECAB_VERSION}.tar.gz"
tar xf "mecab-${MECAB_VERSION}.tar.gz"
cd "mecab-${MECAB_VERSION}"
./configure --prefix=/usr/local --with-charset=utf8
make
make install
cd /tmp
rm -rf "mecab-${MECAB_VERSION}" "mecab-${MECAB_VERSION}.tar.gz"

# Build and install MeCab IPA dictionary
cd /tmp
wget "https://github.com/shogo82148/mecab/releases/download/v${MECAB_VERSION}/mecab-ipadic-${MECAB_IPADIC_VERSION}.tar.gz"
tar xf "mecab-ipadic-${MECAB_IPADIC_VERSION}.tar.gz"
cd "mecab-ipadic-${MECAB_IPADIC_VERSION}"
./configure --prefix=/usr/local --with-charset=utf8 --with-mecab-config=/usr/local/bin/mecab-config
make
make install
cd /tmp
rm -rf "mecab-ipadic-${MECAB_IPADIC_VERSION}" "mecab-ipadic-${MECAB_IPADIC_VERSION}.tar.gz"

# Build and install Groonga with MeCab support
cd /tmp
wget "https://packages.groonga.org/source/groonga/groonga-${GROONGA_VERSION}.tar.gz"
tar xf "groonga-${GROONGA_VERSION}.tar.gz"
cd "groonga-${GROONGA_VERSION}"
cmake \
  -S . \
  -B ../groonga.build \
  --preset=release-maximum \
  -DCMAKE_INSTALL_PREFIX=/usr/local \
  -DGRN_WITH_MRUBY=OFF \
  -DGRN_WITH_MECAB=ON
cmake --build ../groonga.build
cmake --install ../groonga.build
cd /tmp
rm -rf "groonga-${GROONGA_VERSION}" "groonga-${GROONGA_VERSION}.tar.gz" groonga.build

# Build and install PGroonga
cd /tmp
wget "https://packages.groonga.org/source/pgroonga/pgroonga-${PGROONGA_VERSION}.tar.gz"
tar xf "pgroonga-${PGROONGA_VERSION}.tar.gz"
cd "pgroonga-${PGROONGA_VERSION}"
make HAVE_MSGPACK=1 HAVE_XXHASH=1 \
  PG_CPPFLAGS="-I/usr/local/include/groonga -DPGRN_VERSION='\"${PGROONGA_VERSION}\"'" \
  SHLIB_LINK="-L/usr/local/lib -lgroonga"
make install
cd /tmp
rm -rf "pgroonga-${PGROONGA_VERSION}" "pgroonga-${PGROONGA_VERSION}.tar.gz"
