# Python 缓存策略

这是一篇用于离线检索测试的固定语料，不代表真实博客文章。

## cachetools.TTLCache

`cachetools.TTLCache` 适合进程内的短期缓存。它的内容不会自动在多个进程之间同步，因此不能替代 Redis 这类共享缓存。

```python
from cachetools import TTLCache

recent_articles = TTLCache(maxsize=128, ttl=60)
```

## 连接配置

HTTP 客户端应复用连接池，并为连接与读取分别设置超时。不同上游服务的失败不能被缓存为永久结果。

## 缓存穿透

对于不存在的资源可以缓存短生命周期的空结果，但需要区分临时故障与确实不存在，避免隐藏上游错误。