set -e

sudo dpkg --purge kubby
sudo ./build-deb.sh
sudo apt install ../*.deb
