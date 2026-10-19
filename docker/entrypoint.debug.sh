#!/bin/sh
set -eu

LAY4FLOW_PROFILE_BACKGROUND_DISPLAY="${LAY4FLOW_PROFILE_BACKGROUND_DISPLAY:-}"

if [ -n "${LAY4FLOW_PROFILE_BACKGROUND_DISPLAY:-}" ]; then
    foreground_number="${DISPLAY:-:99}"
    foreground_number="${foreground_number#:}"
    foreground_number="${foreground_number%%.*}"
    background_number="${LAY4FLOW_PROFILE_BACKGROUND_DISPLAY#:}"
    background_number="${background_number%%.*}"
    case "${background_number}" in
        ''|*[!0-9]*)
            echo "[debug] invalid background display" >&2
            exit 1
            ;;
    esac
    if [ "${background_number}" = "${foreground_number}" ]; then
        echo "[debug] background display must differ from viewer display" >&2
        exit 1
    fi
    rm -f "/tmp/.X${background_number}-lock" "/tmp/.X11-unix/X${background_number}"
    Xvfb "${LAY4FLOW_PROFILE_BACKGROUND_DISPLAY}" -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb-profile-background.log 2>&1 &
    if command -v xdotool >/dev/null 2>&1; then
        ready=0
        for _ in $(seq 1 50); do
            if DISPLAY="${LAY4FLOW_PROFILE_BACKGROUND_DISPLAY}" xdotool getdisplaygeometry >/dev/null 2>&1; then
                ready=1
                break
            fi
            sleep 0.2
        done
        if [ "${ready}" -ne 1 ]; then
            echo "[debug] background Xvfb did not become ready" >&2
            exit 1
        fi
    fi
fi

echo "[debug] starting x11vnc on ${DISPLAY:-:99}, loopback port 5900"
xvfb_display="${DISPLAY:-:99}"
if command -v xdotool >/dev/null 2>&1; then
    ready=0
    for _ in $(seq 1 50); do
        if DISPLAY="${xvfb_display}" xdotool getdisplaygeometry >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 0.2
    done
    if [ "${ready}" -ne 1 ]; then
        echo "[debug] Xvfb did not become ready" >&2
        exit 1
    fi
fi
x11vnc \
    -display "${DISPLAY:-:99}" \
    -localhost \
    -rfbport 5900 \
    -forever \
    -shared \
    -nopw \
    -noxrecord \
    -noxfixes \
    -noxdamage \
    >/tmp/x11vnc.log 2>&1 &

echo "[debug] starting lay4flow-remote"
exec python main.py
