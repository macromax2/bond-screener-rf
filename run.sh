#!/bin/bash
# Запуск скринера облигаций РФ.
# Процесс не привязан к терминалу: переживает закрытие окна/сессии.
#
#   ./run.sh          — запустить (порт 8000)
#   ./run.sh stop     — остановить
#   ./run.sh status   — проверить, работает ли
#
# Лог: data/server.log   PID: data/server.pid

cd "$(dirname "$0")"
PORT="${PORT:-8000}"
PIDFILE="data/server.pid"
LOGFILE="data/server.log"
mkdir -p data

running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

case "${1:-start}" in
  status)
    if running; then
      echo "работает (pid $(cat "$PIDFILE"), порт $PORT): http://127.0.0.1:$PORT/"
    else
      echo "остановлен"
      exit 1
    fi
    ;;

  stop)
    if running; then
      kill "$(cat "$PIDFILE")"
      rm -f "$PIDFILE"
      echo "остановлен"
    else
      echo "уже остановлен"
    fi
    ;;

  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;

  start)
    if running; then
      echo "уже работает (pid $(cat "$PIDFILE")): http://127.0.0.1:$PORT/"
      exit 0
    fi
    nohup .venv/bin/uvicorn app.main:app --port "$PORT" >>"$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    # ждём, пока поднимется и ответит health-check
    for i in $(seq 1 30); do
      if curl -sf -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
        echo "запущен (pid $(cat "$PIDFILE")): http://127.0.0.1:$PORT/"
        exit 0
      fi
      sleep 1
    done
    echo "сервер не ответил за 30 с — смотри $LOGFILE" >&2
    exit 1
    ;;
esac
