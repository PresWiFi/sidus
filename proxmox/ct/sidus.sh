#!/usr/bin/env bash
_CS_DEFAULT_URL="https://raw.githubusercontent.com/PresWiFi/sidus/main/proxmox"
_cs_boot="${COMMUNITY_SCRIPTS_CORE_DIR:-$(dirname "${BASH_SOURCE[0]}")/../../core}/core/build.func"
source "$_cs_boot" 2>/dev/null || source <(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL:-https://raw.githubusercontent.com/community-scripts/core/main}/core/build.func")
# Sidus's banner until community-scripts generates one (drop this when submitting).
_CS_FALLBACK_HEADER='   _____ _     __
  / ___/(_)___/ /_  _______
  \__ \/ / __  / / / / ___/
 ___/ / / /_/ / /_/ (__  )
/____/_/\__,_/\__,_/____/'
# Copyright (c) 2021-2026 community-scripts ORG
# Author: PresWiFi
# License: MIT | https://github.com/community-scripts/DevScripts/raw/main/LICENSE
# Source: https://github.com/PresWiFi/sidus

APP="Sidus"
var_tags="${var_tags:-ai;dev-tools}"
var_cpu="${var_cpu:-2}"
var_ram="${var_ram:-4096}"
var_disk="${var_disk:-16}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
#var_arm64="${var_arm64:-no}" # unset = ask the user; set yes/no only when verified
var_unprivileged="${var_unprivileged:-1}"

header_info "$APP"
variables
color
catch_errors

# Sidus was called Hangar until 2026-11. Moves an old install's data, settings, and service to the new names;
# the update below then installs Sidus into /opt/sidus. tmux sessions keep running (KillMode=process).
function migrate_from_hangar() {
  msg_info "Moving Hangar to Sidus"
  systemctl disable -q --now hangar
  mv /opt/hangar_data /opt/sidus_data
  mv /opt/sidus_data/hangar.env /opt/sidus_data/sidus.env
  sed -i -e 's/^HANGAR_/SIDUS_/' -e 's#/opt/hangar_data#/opt/sidus_data#g' /opt/sidus_data/sidus.env
  sed -e 's/Hangar/Sidus/g' -e 's/hangar/sidus/g' /etc/systemd/system/hangar.service >/etc/systemd/system/sidus.service
  rm -f /etc/systemd/system/hangar.service
  if [[ -d /etc/systemd/system/hangar.service.d ]]; then
    mv /etc/systemd/system/hangar.service.d /etc/systemd/system/sidus.service.d
    sed -i 's/HANGAR_/SIDUS_/g' /etc/systemd/system/sidus.service.d/*.conf
  fi
  systemctl daemon-reload
  systemctl enable -q sidus
  rm -rf /opt/hangar
  mkdir -p /opt/sidus
  msg_ok "Moved Hangar to Sidus"
}

function update_script() {
  header_info
  check_container_storage
  check_container_resources

  if [[ ! -d /opt/sidus && -d /opt/hangar ]]; then
    migrate_from_hangar
  fi
  if [[ ! -d /opt/sidus ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi

  msg_info "Updating Claude Code"
  $STD npm install -g @anthropic-ai/claude-code@latest
  msg_ok "Updated Claude Code"

  if check_for_gh_release "sidus" "PresWiFi/sidus"; then
    msg_info "Stopping Sidus"
    systemctl stop sidus
    msg_ok "Stopped Sidus"

    CLEAN_INSTALL=1 fetch_and_deploy_gh_release "sidus" "PresWiFi/sidus" "tarball"

    msg_info "Installing Sidus Dependencies"
    cd /opt/sidus
    $STD uv venv /opt/sidus/.venv
    $STD uv pip install -p /opt/sidus/.venv/bin/python -r requirements.txt
    msg_ok "Installed Sidus Dependencies"

    msg_info "Starting Sidus"
    systemctl start sidus
    msg_ok "Started Sidus"
    msg_ok "Updated successfully!"
  fi
  exit
}

start
build_container
description

msg_ok "Completed Successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW}Access it using the following URL:${CL}"
echo -e "${GATEWAY}${BGN}http://${IP}:7878${CL}"
echo -e "${INFO}${YW}Enter the setup code from Sidus's log on the first visit. Show it with:${CL}"
echo -e "${TAB}${BGN}pct exec ${CTID} -- journalctl -u sidus -o cat | grep -A1 'enter this code'${CL}"
