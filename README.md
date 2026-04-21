# GalTransl for Kamitsubaki Portable

一个面向神椿系长视频字幕处理的可迁移源码版工作仓库。

这个仓库保留了我当前整理好的自动化链路，重点是：

- 长视频日语 ASR
- 字幕弱化检测
- 坏段切片重跑
- 重试失败后的保留并继续
- GPT 主翻译 + 本地 Sakura 回退
- headless 长期监听运行

这个版本的目标不是打包即用，而是方便在别的电脑上快速复刻同一套工作流。

## 当前特性

- 支持 `watch` 模式监听 `project/inbox`
- 支持 `once` 模式单文件处理
- 支持 Whisper ASR 后的质量检查
- 支持识别连续重复字幕和单句字符刷屏
- 支持坏段切片重跑与超长重复分块修复
- 支持修复失败后保留裁剪结果继续流程，尽量保证任务最终完成
- 支持 OpenAI 兼容接口作为主翻译
- 支持在线接口反复 `503` 时自动回退到本地 Sakura
- 支持后台运行并输出 `project/watch.log`

## 仓库定位

这是源码版 portable 仓库，不包含以下内容：

- 私有 API key
- 模型权重
- 运行产物和缓存
- 本地打包后的大二进制

你需要在自己的机器上准备运行时文件和模型。

## 快速开始

1. 克隆仓库
2. 安装 Python 依赖
3. 准备运行时工具和模型
4. 配置 `project/api_key.txt`
5. 检查 `project/headless_config.yaml`
6. 启动长期监听

```bash
bash run_server.sh
```

查看日志：

```bash
tail -f project/watch.log
```

停止：

```bash
bash stop_server.sh
```

更完整的迁移说明见 [PORTABLE_SETUP.md](PORTABLE_SETUP.md)。

## 目录说明

- `headless_server.py`
  当前主要的长期运行入口
- `run_server.sh`
  启动 watcher
- `stop_server.sh`
  停止 watcher
- `project/headless_config.yaml`
  headless 工作流配置
- `project/config.yaml`
  GalTransl 运行配置模板
- `GalTransl/SubtitleQuality.py`
  字幕弱化检测逻辑

## 许可证与来源

本仓库是基于上游项目二次整理和修改后的源码版工作仓库。

- 上游之一：`VoiceTransl`
- 上游基础：`GalTransl`

由于上游代码链路受 GPLv3 约束，这个仓库继续沿用 GPLv3。  
具体说明见 [NOTICE.md](NOTICE.md) 和 [LICENSE](LICENSE)。
