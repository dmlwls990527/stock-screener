# -*- coding: utf-8 -*-
"""tb_conn.py — Tibero 접속 정보를 tip 파일에서 자동으로 읽어온다.

배경: 서버가 재설치될 때마다 tip이 새로 생성되며 LISTENER_PORT가 바뀐다
      (2026-07-30: 44123 -> 31231). 그때마다 12개 파일을 sed로 고치던 문제를 없앴다.

우선순위:
  1) 환경변수 TIBERO_PORT (명시적 지정)
  2) 주식 DB tip 파일의 LISTENER_PORT   <- 평소 여기서 자동 해결
  3) 환경변수 TB_HOME/TB_SID 로 지정된 tip
  4) DEFAULT_PORT

주의: 이 서버에는 회사 클러스터(tac0/tac1/tsc1...)도 함께 돌아서, 셸의 TB_HOME이
      다른 인스턴스를 가리킬 수 있다. 그래서 주식 DB 경로를 먼저 본다.
"""
import os
import re

STOCK_TB_HOME = os.environ.get("STOCK_TB_HOME") or "/data/tibero7/tibero7"
STOCK_TB_SID = os.environ.get("STOCK_TB_SID") or "tibero"
DEFAULT_PORT = 31231


def port_from_tip(tb_home, tb_sid):
    """$TB_HOME/config/$TB_SID.tip 에서 LISTENER_PORT 숫자만 뽑는다.
    주석(#) 줄은 무시하고, 값이 여러 번 나오면 마지막 것을 쓴다(tip 해석 규칙과 동일).
    파일이 없거나 값이 없으면 None."""
    tip = os.path.join(tb_home, "config", tb_sid + ".tip")
    port = None
    try:
        with open(tip, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                m = re.match(r"LISTENER_PORT\s*=\s*(\d+)", s)
                if m:
                    port = int(m.group(1))
    except OSError:
        return None
    return port


def resolve_port():
    """(포트, 출처) 반환."""
    env = (os.environ.get("TIBERO_PORT") or "").strip()
    if env.isdigit():
        return int(env), "env:TIBERO_PORT"

    p = port_from_tip(STOCK_TB_HOME, STOCK_TB_SID)
    if p:
        return p, "tip:%s/config/%s.tip" % (STOCK_TB_HOME, STOCK_TB_SID)

    tb_home, tb_sid = os.environ.get("TB_HOME"), os.environ.get("TB_SID")
    if tb_home and tb_sid:
        p = port_from_tip(tb_home, tb_sid)
        if p:
            return p, "tip:env TB_HOME/%s.tip" % tb_sid

    return DEFAULT_PORT, "default(fallback)"


PORT, PORT_SRC = resolve_port()
HOST = os.environ.get("TIBERO_HOST", "localhost")
SID = STOCK_TB_SID
URL = "jdbc:tibero:thin:@%s:%d:%s" % (HOST, PORT, SID)
JAR = os.environ.get("TIBERO_JAR") or os.path.join(STOCK_TB_HOME, "client/lib/jar/tibero7-jdbc.jar")
USER = os.environ.get("TIBERO_USER", "sys")
PASS = os.environ.get("TIBERO_PASS", "")

if __name__ == "__main__":
    print("PORT = %s   (출처: %s)" % (PORT, PORT_SRC))
    print("URL  =", URL)
    print("JAR  =", JAR, "[있음]" if os.path.exists(JAR) else "[없음!]")
    try:
        import jaydebeapi as j
        c = j.connect("com.tmax.tibero.jdbc.TbDriver", URL, [USER, PASS], JAR)
        cur = c.cursor(); cur.execute("SELECT 1 FROM dual")
        print("접속 테스트: OK", cur.fetchone())
        c.close()
    except Exception as e:
        print("접속 테스트: 실패 -", str(e)[:150])