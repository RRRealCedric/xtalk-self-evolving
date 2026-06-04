# xtalk.log_utils

## mute_other_logging

```python
def mute_other_logging()
```

降低 Xtalk 所使用的第三方日志器噪声。

### Notes

该辅助函数会将根日志器级别提升到 `WARNING`，并对常见的网络层和 SDK 日志器应用相同阈值，这样示例应用可以将终端输出集中在 Xtalk 事件上。

## setup_logging

```python
def setup_logging()
```

配置进程级 Xtalk 日志器。

### Returns

- `logging.Logger`
  已配置好的 `xtalk` 日志器实例。

### Notes

每次进程启动时，都会在 `logs/` 下创建一个带时间戳的日志文件。

## logger

```python
logger
```

**Value:** `setup_logging()`
