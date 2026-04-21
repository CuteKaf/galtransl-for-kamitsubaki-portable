# GalTransl for Kamitsubaki Portable

一个面向神椿系长视频、直播回放和字幕整理场景的 portable 源码版工作仓库。

这个仓库的目标很明确：把我现在实际在用、已经补过很多坑的字幕流水线保存下来，方便以后在别的电脑上直接复刻，而不是每次重新调环境、重新踩一次长视频 ASR 和翻译回退的问题。

## 这版重点做了什么

- 长视频日语 ASR
- 字幕弱化检测
- 坏段切片重跑
- 超长重复片段分块修复
- 修复失败后的保留并继续
- GPT 主翻译 + 本地 Sakura 自动回退
- `headless` 长期监听运行

## 当前能力

- 支持 `watch` 模式监听 `project/inbox`
- 支持 `once` 模式单文件处理
- 支持 Whisper ASR 后的质量检查
- 支持识别连续重复字幕和单句字符刷屏
- 支持坏段切片重跑与超长重复分块修复
- 支持修复失败后保留裁剪结果继续流程，优先保证任务能落成品
- 支持 OpenAI 兼容接口作为主翻译
- 支持在线接口反复 `503` 时自动回退到本地 Sakura
- 支持后台运行并输出 `project/watch.log`

## 适合什么场景

- 神椿系长直播回放字幕整理
- 日语音视频的自动听写 + 翻译
- 需要长时间挂机处理的批量字幕任务
- 想把本地工作流迁移到别的电脑继续复用

## 仓库定位

这是源码版 portable 仓库，不包含以下内容：

- 私有 API key
- 模型权重
- 运行产物和缓存
- 本地打包后的大二进制

所以你拿到仓库之后，还是需要在自己的机器上准备运行时文件和模型。

## 快速开始

1. 克隆仓库
2. 安装 Python 依赖
3. 准备运行时工具和模型
4. 复制 `project/api_key.example.txt` 为 `project/api_key.txt`
5. 检查 `project/headless_config.yaml`
6. 启动长期监听

启动：

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

## 关键入口

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

本仓库是基于上游项目继续整理和修改后的源码版工作仓库。

- 上游之一：`VoiceTransl`
- 上游基础：`GalTransl`

由于上游代码链路受 GPLv3 约束，这个仓库继续沿用 GPLv3。  
具体说明见 [NOTICE.md](NOTICE.md) 和 [LICENSE](LICENSE)。
