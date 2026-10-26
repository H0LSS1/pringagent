"""采集服务入口（命令行程序）。

【零基础说明】
Python 的 `-m` 参数表示"把某个包当程序运行"：`python -m collector.main`
会执行 collector/main.py 的 main()。这样写而不是直接跑 .py 文件，
是因为包内的相对导入（from collector.poller import Poller）需要包上下文。

用法（backend 目录下）：
    .venv\\Scripts\\python -m collector.main                # 7x24 常驻轮询
    .venv\\Scripts\\python -m collector.main --cycles 10    # 跑 10 个周期后退出（联调用）
    .venv\\Scripts\\python -m collector.main --verbose      # 打印 Modbus 报文级调试日志
"""

import argparse
import logging

from collector.poller import Poller


def main() -> None:
    ap = argparse.ArgumentParser(description="PrintAgent Modbus 采集服务")
    ap.add_argument("--cycles", type=int, default=None,
                    help="轮询周期数（默认无限常驻）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    poller = Poller()
    try:
        poller.run(cycles=args.cycles)
    except KeyboardInterrupt:
        logging.info("stopped by user")
    finally:
        # 无论正常退出还是 Ctrl+C，都把缓冲区里剩余的数据落库，不丢数据
        poller.flush(force=True)
        logging.info("final stats: %s", poller.stats)


if __name__ == "__main__":
    main()
