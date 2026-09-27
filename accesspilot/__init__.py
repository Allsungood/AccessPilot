"""AccessPilot - 面向 ChatGPT / Discord / X 等平台的网络加速与分流管理器.

设计原则:
  1. 不重复造内核。代理协议栈复用 mihomo(Clash.Meta) 这一成熟开源内核。
  2. 本工具负责内核不具备的能力: 订阅转换、定向分流、系统集成、可用性诊断。
  3. 零第三方依赖(Python 标准库即可运行), 降低安装门槛与供应链风险。
"""

__version__ = "1.0.0"
APP_NAME = "AccessPilot"
