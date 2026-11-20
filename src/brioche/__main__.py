import argparse
import signal
import sys
import time


def main():
    parser = argparse.ArgumentParser(description="Brioche encrypted remote desktop")
    sub = parser.add_subparsers(dest="command")
    host = sub.add_parser("host", help="Start sharing the logged-in desktop")
    host.add_argument("--bind", default="127.0.0.1")
    host.add_argument("--advertise", default="127.0.0.1", help="Reachable LAN or Tailscale host address")
    host.add_argument("--port", type=int, default=47995)
    host.add_argument("--encoder", choices=("auto", "libx264", "h264_nvenc", "h264_qsv", "h264_videotoolbox"), default="libx264")
    host.add_argument("--monitor", type=int, default=1)
    host.add_argument("--view-only", action="store_true")
    host.add_argument("--state-dir")
    host.add_argument("--web", action="store_true", help="Enable the HTTPS browser client on the selected address")
    host.add_argument("--web-port", type=int, default=47996)
    sub.add_parser("doctor", help="Inspect capture, codec and desktop prerequisites")
    check = sub.add_parser("self-test", help="Check packaged runtime without capturing or controlling a desktop")
    check.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "self-test":
        from .bundle_check import run
        return run(args.output)
    if args.command == "doctor":
        from .diagnostics import doctor
        return doctor()
    if args.command == "host":
        from .session import Host
        from .security import access_uri
        server = Host(args.bind, args.port, args.encoder, args.monitor, print, args.state_dir, args.view_only,
                      web_enabled=args.web, web_port=args.web_port)
        server.start()
        if not server.ready.wait(15) or server.error:
            server.stop()
            print(server.error or "启动超时", file=sys.stderr)
            return 1
        print("连接凭据包含控制权限，仅通过可信渠道传递：", flush=True)
        print(access_uri(args.advertise, server.port, server.pin, server.token), flush=True)
        if server.browser_url:
            print("Browser URL:", server.browser_url, flush=True)
        def stop(*unused):
            server.stop()
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        try:
            while server._thread.is_alive():
                time.sleep(0.2)
        finally:
            server.stop()
            server.join()
        return 0
    from .ui import run
    return run()


if __name__ == "__main__":
    sys.exit(main())
