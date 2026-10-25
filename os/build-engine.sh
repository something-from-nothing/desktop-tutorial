#!/bin/bash
# Builds llama-server for Desk OS inside debian:trixie, so it matches the system's
# libraries. Every CPU variant is built and the right one is picked when it starts,
# so it's fast on new chips and still runs on old Celerons.
set -euo pipefail
cd "$(dirname "$0")"
apt-get update
apt-get install -y --no-install-recommends cmake ninja-build g++ git ca-certificates
cmake -S ../llama.cpp -B engine-build -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON -DBUILD_SHARED_LIBS=ON \
  -DGGML_OPENMP=OFF -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
  -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON '-DCMAKE_INSTALL_RPATH=$ORIGIN'
cmake --build engine-build -j "$(nproc)"
rm -rf engine && mkdir engine
cp engine-build/bin/llama-server engine/
cp -a engine-build/bin/*.so* engine/  
ls -la engine
ldd engine/llama-server
