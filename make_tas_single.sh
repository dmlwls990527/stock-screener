#!/usr/bin/env bash
# 반드시 bash 로 실행:  bash make_tas_single.sh
# (sh 로 실행 시 dash 가 떠서 local / (( )) 등에서 중단될 수 있음)

if [ -z "${BASH_VERSION:-}" ]; then
  exec bash "$0" "$@"
fi

set -eu

# ----- Tibero 버전 선택 (t6.profile / t7.profile) -----
while true; do
  echo ""
  echo "Tibero 버전을 선택하세요. 6 또는 7 [기본: 7]"
  read -r tb_version
  tb_version=${tb_version:-7}
  if [ "$tb_version" = "6" ]; then
    echo "Tibero Version: T6"
    [ -f ./t6.profile ] && . ./t6.profile || echo "[WARN] t6.profile 없음, TB_HOME 등은 환경변수/기본값 사용"
    break
  elif [ "$tb_version" = "7" ]; then
    echo "Tibero Version: T7"
    [ -f ./t7.profile ] && . ./t7.profile || echo "[WARN] t7.profile 없음, TB_HOME 등은 환경변수/기본값 사용"
    break
  else
    echo "6 또는 7을 입력하세요."
    sleep 1
  fi
done

# =============================================================================
# TAS + 단일 인스턴스 DB 구축 (멀티노드 TAC / TSC 스탠바이 없음)
# =============================================================================
#   bash make_tas_single.sh
#   NON_INTERACTIVE=1 HOST_NAME=192.168.x.x TAS_REDUN=2 ... bash make_tas_single.sh  # 프롬프트 생략
#
#   make_tas_tac_tsc.sh 의 검증된 node-0 경로를 기반으로, 단일 인스턴스용으로 축소.
#   기존 대비 차이: ① 단일 노드  ② DB CLUSTER_DATABASE=N  ③ TSC 없음
#
#   source t7.profile 후 실행 권장
# =============================================================================

# ----- DB / 접속 -----
TB_HOME=${TB_HOME:-/data/tibero7/tibero7}
CM_HOME=${CM_HOME:-$TB_HOME}
TB_CONFIG_DIR="$TB_HOME/config"
TB_DSN_FILE="$TB_HOME/client/config/tbdsn.tbr"
TB_LICENSE_DIR="$TB_HOME/license"
DB_NAME="tibero"
SYS_PASS="tibero"

# 단일 인스턴스 = 진짜 비클러스터(N). CM 관리하의 1노드 클러스터로 두려면 Y 로.
DB_CLUSTER_DATABASE=${DB_CLUSTER_DATABASE:-N}

# ----- TAS 디스크 (disk1,disk2,... 만 이 디렉터리에 둠) -----
TAS_DISK=${TAS_DISK:-}
# make_mj.sh 처럼 ds.sql 은 디스크 경로 밖에 둠 — TAS_DISK/* 쓰면 ds.sql 까지 디스크로 잡혀 오류남
TAS_DDL_DIR=${TAS_DDL_DIR:-$HOME/tas_ds0_ddl}
TAS_REDUN=${TAS_REDUN:-}
TAS_DISK_CNT=${TAS_DISK_CNT:-}
TAS_DISK_SIZE_GB=${TAS_DISK_SIZE_GB:-}

# ----- 호스트 -----
HOST_NAME=${HOST_NAME:-}

# ----- 포트 (단일 노드이므로 고정값) -----
PORT_DB_LISTENER=${PORT_DB_LISTENER:-22022}    # DB(tac0) 리스너
PORT_TAS_LISTENER=${PORT_TAS_LISTENER:-33033}  # TAS(tas0) 리스너 = DB 의 AS_PORT
PORT_CM_UI=${PORT_CM_UI:-44044}                # CM UI
PORT_DB_CLUSTER=${PORT_DB_CLUSTER:-22011}      # DB local cluster
PORT_TAS_CLUSTER=${PORT_TAS_CLUSTER:-34022}    # TAS local cluster
PORT_CM_NET=${PORT_CM_NET:-55055}              # CM private net

# =============================================================================
log() { echo "[INFO] $1"; }

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "[ERROR] not found: $1"; exit 1; }
}

check_hpux() {
  tbboot -v 2>/dev/null | grep -q "HP-UX" && echo 1 || echo 0
}

# 엔터 시 사용할 기본 IP 자동 감지 (ip addr / ifconfig 에서 127 제외)
detect_default_ip() {
  local ip
  ip=$(ip -4 addr show 2>/dev/null | grep 'inet ' | grep -v 127.0.0.1 | awk '{print $2}' | cut -d/ -f1 | head -1)
  [ -n "$ip" ] && echo "$ip" && return
  echo "localhost"
}

# 대화형: ① 호스트 → ② TAS(redun·경로·개수·크기) → ③ 확인
interactive_config() {
  local ans default_ip
  default_ip=$(detect_default_ip)
  echo ""
  echo "========== 1) 네트워크 =========="
  echo -n "HOST_NAME [엔터= $default_ip]: "
  read -r ans
  HOST_NAME=${ans:-$default_ip}
  if [ "$HOST_NAME" = "localhost" ] || [ "$HOST_NAME" = "127.0.0.1" ]; then
    echo "  ※ localhost 사용 시 CM net1 이 DOWN 될 수 있음. 실제 IP 권장 (ifconfig | grep 192)"
  fi

  echo ""
  echo "========== 2) TAS 디스크 (DISKSPACE DS0) =========="
  echo "  [REDUN]   미러링 방식 (데이터를 몇 군데에 복사할지)"
  echo "            1=EXTERNAL(미러없음)  2=NORMAL(2-way)  3=HIGH(3-way)"
  echo "  [개수]    dd 로 만들 디스크 '파일' 개수. REDUN의 배수여야 함."
  echo "            예: REDUN=2 이면 2,4,6,... / REDUN=3 이면 3,6,9,..."
  while true; do
    echo -n "TAS_REDUN (1|2|3) [기본: 2]: "
    read -r ans
    TAS_REDUN=${ans:-2}
    [[ "$TAS_REDUN" =~ ^[123]$ ]] && break
    echo "  → 1, 2, 3 중 선택."
  done

  echo -n "TAS 디스크 디렉터리 [기본: \$HOME/disk]: "
  read -r ans
  TAS_DISK=${ans:-$HOME/disk}
  mkdir -p "$TAS_DISK" || { echo "[ERROR] 디렉터리 생성 실패: $TAS_DISK"; exit 1; }

  while true; do
    echo -n "TAS 디스크 파일 개수 (${TAS_REDUN}의 배수, 예: ${TAS_REDUN},$((TAS_REDUN*2)),...) [기본: $TAS_REDUN]: "
    read -r ans
    TAS_DISK_CNT=${ans:-$TAS_REDUN}
    [[ "$TAS_DISK_CNT" =~ ^[0-9]+$ ]] && [ $((TAS_DISK_CNT % TAS_REDUN)) -eq 0 ] && [ "$TAS_DISK_CNT" -ge "$TAS_REDUN" ] && break
    echo "  → $TAS_REDUN 의 배수여야 합니다."
  done

  while true; do
    echo -n "디스크 파일 1개당 크기 GB (2~100, 권장 10 이상) [기본: 10]: "
    read -r ans
    TAS_DISK_SIZE_GB=${ans:-10}
    [[ "$TAS_DISK_SIZE_GB" =~ ^[0-9]+$ ]] && [ "$TAS_DISK_SIZE_GB" -ge 2 ] && [ "$TAS_DISK_SIZE_GB" -le 100 ] && break
    echo "  → 2~100 사이 숫자."
  done

  echo ""
  echo "========== 3) 요약 (계속하려면 y) =========="
  echo "  구성               = TAS + 단일 DB(tac0)  [CLUSTER_DATABASE=$DB_CLUSTER_DATABASE]"
  echo "  HOST_NAME          = $HOST_NAME"
  echo "  TAS_DISK           = $TAS_DISK  (disk1,disk2,... 만)"
  echo "  TAS_DDL_DIR        = $TAS_DDL_DIR  (ds.sql 위치, 디스크와 분리)"
  echo "  TAS_REDUN          = $TAS_REDUN (미러 수)"
  echo "  디스크 파일 개수   = $TAS_DISK_CNT 개"
  echo "  파일당 크기        = ${TAS_DISK_SIZE_GB} GB  →  총 용량 약 $((TAS_DISK_CNT * TAS_DISK_SIZE_GB)) GB"
  echo -n "진행할까요? (Y/N) [엔터=진행]: "
  read -r ans
  ans=$(echo "$ans" | tr '[:upper:]' '[:lower:]')
  [[ -z "$ans" || "$ans" == "y" || "$ans" == "yes" ]] || { echo "취소됨."; exit 0; }
}

apply_defaults_noninteractive() {
  # CM 은 실제 IP 필요 — localhost 이면 net1 DOWN
  HOST_NAME=${HOST_NAME:-}
  TAS_DISK=${TAS_DISK:-$HOME/disk}
  TAS_REDUN=${TAS_REDUN:-2}
  TAS_DISK_CNT=${TAS_DISK_CNT:-2}
  TAS_DISK_SIZE_GB=${TAS_DISK_SIZE_GB:-10}
}

# ----- tbdown/tbcm 정리 시 메시지 숨김 -----
silent_tbdown() {
  if has_cmd tbdown; then
    TB_SID=$1 tbdown abnormal >/dev/null 2>&1 || true
  else
    :
  fi
}
silent_tbcm_d() { CM_SID=$1 tbcm -d >/dev/null 2>&1 || true; }

clean_all() {
  log "Clean TAS/DB (인스턴스 정리 중…)"
  local c _
  # 단일 노드지만 과거 멀티노드 잔재까지 안전하게 정리
  for c in 0 1 2 3 4 5 6 7 8 9; do
    silent_tbdown "tac$c"
    silent_tbdown "tas$c"
    silent_tbcm_d "cm$c"
    sleep 1
  done
  for _ in {1..10}; do
    pids=$(ps -ef 2>/dev/null | grep "$(whoami)" | grep tbcm | grep -v grep | awk '{print $2}' || true)
    [ -z "$pids" ] && break
    for pid in $pids; do kill -9 "$pid" 2>/dev/null || true; done
    sleep 1
  done
  mkdir -p "$TAS_DISK"
  rm -f "$TAS_DISK"/disk[0-9]* "$TAS_DISK"/disk[0-9][0-9]* 2>/dev/null || true
  rm -f "$TAS_DISK"/ds.sql "$TAS_DISK"/ds_force.sql 2>/dev/null || true
  rm -f "$TAS_DDL_DIR"/ds.sql "$TAS_DDL_DIR"/ds_force.sql 2>/dev/null || true
  rm -rf "$TB_HOME/instance"
  rm -f "$CM_HOME"/cm*
  rm -f "$TB_CONFIG_DIR"/*.tip
  rm -f "$TB_DSN_FILE"
}

create_tas_disk_and_ddl() {
  log "TAS raw 디스크 생성 ($TAS_DISK, dd …)"
  mkdir -p "$TAS_DISK" "$TAS_DDL_DIR"
  local dd_bs=$((TAS_DISK_SIZE_GB * 1024)) n redun_word ds dsf disk_no fg i per_fg
  for ((n = 1; n <= TAS_DISK_CNT; n++)); do
    dd if=/dev/zero of="$TAS_DISK/disk$n" bs="$dd_bs" count=1048576 status=none 2>/dev/null || \
      dd if=/dev/zero of="$TAS_DISK/disk$n" bs="$dd_bs" count=1048576
  done
  case "$TAS_REDUN" in 1) redun_word="EXTERNAL" ;; 3) redun_word="HIGH" ;; *) redun_word="NORMAL" ;; esac
  ds="$TAS_DDL_DIR/ds.sql"
  dsf="$TAS_DDL_DIR/ds_force.sql"
  : > "$ds"
  : > "$dsf"
  echo "CREATE DISKSPACE DS0 $redun_word REDUNDANCY" >> "$ds"
  echo "CREATE DISKSPACE DS0 FORCE $redun_word REDUNDANCY" >> "$dsf"
  disk_no=1
  per_fg=$((TAS_DISK_CNT / TAS_REDUN))
  for ((fg = 1; fg <= TAS_REDUN; fg++)); do
    echo "FAILGROUP FG${fg} DISK" >> "$ds"
    echo "FAILGROUP FG${fg} DISK" >> "$dsf"
    for ((i = 1; i <= per_fg; i++)); do
      if [ "$i" -eq "$per_fg" ]; then
        echo "'$TAS_DISK/disk$disk_no' NAME FG${fg}_DISK${i}" >> "$ds"
        echo "'$TAS_DISK/disk$disk_no' NAME FG${fg}_DISK${i}" >> "$dsf"
      else
        echo "'$TAS_DISK/disk$disk_no' NAME FG${fg}_DISK${i}," >> "$ds"
        echo "'$TAS_DISK/disk$disk_no' NAME FG${fg}_DISK${i}," >> "$dsf"
      fi
      disk_no=$((disk_no + 1))
    done
  done
  echo ";" >> "$ds"
  echo ";" >> "$dsf"
  log "ds.sql 작성 완료"
}

write_cm_tip() {
  log "CM tip (cm0)"
  mkdir -p "$CM_HOME/config"
  local is_hp=$1 f="$CM_HOME/config/cm0.tip"
  : > "$f"
  echo "CM_NAME=cm0" >> "$f"
  echo "CM_UI_PORT=${PORT_CM_UI}" >> "$f"
  echo "CM_RESOURCE_FILE=$CM_HOME/cm0.res" >> "$f"
  [ "$is_hp" -eq 1 ] && echo "_CM_BLOCK_SIZE=1024" >> "$f" || true
}

write_tas_tip() {
  log "tip tas0"
  local is_hp=$1 f="$TB_CONFIG_DIR/tas0.tip"
  cat > "$f" <<EOF
LISTENER_PORT=${PORT_TAS_LISTENER}
THREAD=0
CM_PORT=${PORT_CM_UI}
LOCAL_CLUSTER_PORT=${PORT_TAS_CLUSTER}
MEMORY_TARGET=3G
MAX_SESSION_COUNT=30
TOTAL_SHM_SIZE=2G
CLUSTER_DATABASE=Y
BOOT_WITH_AUTO_DOWN_CLEAN=Y
LOCAL_CLUSTER_ADDR=$HOST_NAME
_SLEEP_ON_SIG=Y
INSTANCE_TYPE=AS
AS_ALLOW_ONLY_RAW_DISKS=N
EOF
  [ "$is_hp" -eq 1 ] && echo "_LOG_BLOCK_SIZE=1024" >> "$f" || true
  echo "AS_DISKSTRING=\"$TAS_DISK/disk*\"" >> "$f"
}

write_db_tip() {
  log "tip tac0 (단일 인스턴스, CLUSTER_DATABASE=$DB_CLUSTER_DATABASE, +DS0)"
  local is_hp=$1 f="$TB_CONFIG_DIR/tac0.tip"
  cat > "$f" <<EOF
LISTENER_PORT=${PORT_DB_LISTENER}
AS_PORT=${PORT_TAS_LISTENER}
CM_PORT=${PORT_CM_UI}
LOCAL_CLUSTER_PORT=${PORT_DB_CLUSTER}
THREAD=0
UNDO_TABLESPACE=UNDO0
DB_NAME=$DB_NAME
LOCAL_CLUSTER_ADDR=$HOST_NAME
CONTROL_FILES=+DS0/c1.ctl
DB_CREATE_FILE_DEST=+DS0
LOG_ARCHIVE_DEST=+DS0/archive
MEMORY_TARGET=4G
MAX_SESSION_COUNT=30
TOTAL_SHM_SIZE=2G
_SLEEP_ON_SIG=Y
USE_ACTIVE_STORAGE=Y
CLUSTER_DATABASE=$DB_CLUSTER_DATABASE
EOF
  [ "$is_hp" -eq 1 ] && echo "_LOG_BLOCK_SIZE=1024" >> "$f" || true
}

write_tbdsn() {
  log "tbdsn.tbr"
  mkdir -p "$(dirname "$TB_DSN_FILE")"
  : > "$TB_DSN_FILE"
  echo "tas0=((INSTANCE=(HOST=$HOST_NAME)(PORT=${PORT_TAS_LISTENER})(DB_NAME=tas)))" >> "$TB_DSN_FILE"
  echo "tac0=((INSTANCE=(HOST=$HOST_NAME)(PORT=${PORT_DB_LISTENER})(DB_NAME=$DB_NAME)))" >> "$TB_DSN_FILE"
}

copy_license() {
  mkdir -p "$TB_LICENSE_DIR"
  if [ -f "./license.xml" ]; then
    cp -f "./license.xml" "$TB_LICENSE_DIR/" && log "license.xml 복사"
  else
    log "license.xml 없음 (cwd) — 스킵"
  fi
}

create_db() {
  log "단일 노드: DS0 + tas0 + tac0"
  export CM_SID=cm0 TB_SID=tac0
  tbcm -b
  sleep 2
  cmrctl add network --nettype private --ipaddr "$HOST_NAME" --portno "$PORT_CM_NET" --name net1
  cmrctl add cluster --incnet net1 --cfile "+$TAS_DISK/disk*" --name cls1
  TB_SID=tas0 tbboot nomount
  sleep 2
  # 최초 1회만 ds.sql 로 DS0 생성. ds_force.sql 은 이미 DS0 가 있을 때만 사용 (지금 실행 시 TBR-2131 발생)
  tbsql "sys/$SYS_PASS@tas0" <<EOF
@${TAS_DDL_DIR}/ds.sql
q
EOF
  cmrctl start cluster --name cls1
  sleep 2
  cmrctl add service --name tas --type as --cname cls1
  sleep 2
  cmrctl add service --name tibero --cname cls1
  sleep 2
  cmrctl add as --name tas0 --svcname tas --dbhome "$CM_HOME"
  cmrctl add db --name tac0 --svcname tibero --dbhome "$CM_HOME"
  TB_SID=tas0 tbboot
  sleep 2
  TB_SID=tac0 tbboot nomount
  tbsql "sys/$SYS_PASS@tac0" <<'EOSQL'
create database "tibero"
user sys identified by tibero
maxinstances 8
maxdatafiles 200
character set MSWIN949
logfile group 0 '+DS0/log000.log' size 50m,
        group 1 '+DS0/log001.log' size 50m,
        group 2 '+DS0/log002.log' size 50m
maxloggroups 255
maxlogmembers 8
archivelog
datafile '+DS0/system001.dtf' size 100M autoextend on next 10M maxsize unlimited
default tablespace usr
datafile '+DS0/usr.dtf' size 100M autoextend on next 10M extent management local autoallocate
default temporary tablespace TEMP
tempfile '+DS0/temp001.dtf' size 100M autoextend on next 10M extent management local autoallocate
undo tablespace UNDO0
datafile '+DS0/undo000.dtf' size 100M autoextend on next 10M extent management local autoallocate;
q
EOSQL
  TB_SID=tac0 tbboot
  sleep 1
  export TB_SID=tac0
  if [ -x "$TB_HOME/scripts/system_install.sh" ]; then
    sh "$TB_HOME/scripts/system_install.sh" -p1 "$SYS_PASS" -p2 syscat
  elif [ -x "$TB_HOME/scripts/system.sh" ]; then
    sh "$TB_HOME/scripts/system.sh" <<EOF
$SYS_PASS
syscat
Y
Y
Y
Y
EOF
    sh "$TB_HOME/scripts/system_install.sh" <<EOF
$SYS_PASS
syscat
EOF
  else
    echo "[ERROR] $TB_HOME/scripts/system_install.sh 없음"
    exit 1
  fi
}

main() {
  require_cmd tbboot
  require_cmd tbsql
  require_cmd tbcm
  require_cmd cmrctl
  require_cmd dd
  if has_cmd tbdown; then
    log "tbdown found"
  else
    log "tbdown not found (optional): continuing without explicit tbdown"
  fi

  if [ "${NON_INTERACTIVE:-0}" = "1" ]; then
    apply_defaults_noninteractive
  else
    interactive_config
  fi

  if [ $((TAS_DISK_CNT % TAS_REDUN)) -ne 0 ]; then
    echo "[ERROR] TAS_DISK_CNT 가 TAS_REDUN 배수여야 함"
    exit 1
  fi

  if [ "${NON_INTERACTIVE:-0}" = "1" ]; then
    hn=$(echo "${HOST_NAME:-}" | tr '[:upper:]' '[:lower:]')
    if [ -z "${HOST_NAME:-}" ] || [ "$hn" = "localhost" ] || [ "$HOST_NAME" = "127.0.0.1" ]; then
      echo "[ERROR] NON_INTERACTIVE 시 HOST_NAME 에 실제 IP 필요. 예: export HOST_NAME=192.168.51.148"
      exit 1
    fi
  fi

  echo ""
  log "TB_HOME=$TB_HOME  구성=TAS+단일DB(CLUSTER_DATABASE=$DB_CLUSTER_DATABASE)  HOST=$HOST_NAME"
  log "TAS $TAS_DISK  REDUN=$TAS_REDUN  DISKS=$TAS_DISK_CNT  ${TAS_DISK_SIZE_GB}GB/disk"

  local IS_HPUX
  IS_HPUX=$(check_hpux)

  clean_all
  create_tas_disk_and_ddl
  write_cm_tip "$IS_HPUX"
  write_tas_tip "$IS_HPUX"
  write_db_tip "$IS_HPUX"
  write_tbdsn
  copy_license
  sleep 2

  create_db

  log "완료: TAS(+DS0) + 단일 DB tac0"
}

main "$@"
