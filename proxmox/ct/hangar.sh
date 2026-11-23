#!/usr/bin/env bash
# Hangar is now Sidus. Containers installed as Hangar still fetch this file when you run `update`;
# it runs the Sidus script, which moves the install to the new names and updates it.
source <(curl -fsSL https://raw.githubusercontent.com/PresWiFi/sidus/main/proxmox/ct/sidus.sh)
