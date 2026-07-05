set -e

sudo dpkg --purge kubby || true
sudo ./build-deb.sh
sudo apt install -y ../*.deb
