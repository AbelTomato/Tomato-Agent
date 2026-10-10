# 本机 OpenShell Gateway

Compose 固定 Gateway 为 `172.30.240.2`，专用网络为 `172.30.240.0/24`。宿主端口仅绑定 `127.0.0.1:18080/18081`。supervisor 使用 host 网络，通过固定地址回连；仅健康接口正常不足以证明此链路正常。

复用本机 `/var/lib/tomato-openshell` 的数据库及 JWT 材料，配置默认位于 `/home/abeltomato/.local/state/openshell/tomato-local/gateway.toml`。可用 `OPENSHELL_GATEWAY_CONFIG_PATH` 指定其他已有配置。Gateway 沿用原部署 UID 0 与 Docker socket；这是可信本地开发部署，当前明文、无认证配置不适用于公开网络。

## 启动与重建

先确认没有活动 Sandbox；配置脚本检查 Docker 网络及宿主 Linux 路由冲突，但不能检测当前未建立的 VPN 路由。迁移到其他环境前还需核对 Windows/WSL/VPN 网段。

```bash
python3 /home/abeltomato/workspace/projects/Tomato-Agent/deploy/openshell/configure.py --apply
docker compose -f /home/abeltomato/workspace/projects/Tomato-Agent/deploy/openshell/compose.yaml up -d --pull never
python3 /home/abeltomato/workspace/projects/Tomato-Agent/deploy/openshell/configure.py
```

脚本从 Compose 模型读取固定 IP，保留原 TOML 内容，仅更新 `grpc_endpoint`，首次写入前备份为同目录 `gateway.toml.before-fixed-network`。不得提交本地配置及认证材料。停止/启动使用 Compose `stop`/`start`；重建使用 `up -d --force-recreate --pull never`。每次升级后执行真实 Sandbox 创建、上传、pytest 和删除验证。

## 本次迁移恢复入口

旧容器 `tomato-openshell-gateway-before-fixed-network` 保持停止，不自动删除。需要回退时，先停止 Compose Gateway，恢复上述 TOML 备份，再启动旧容器；旧容器仍使用动态 bridge IP，因此必须重新核对旧容器 IP 并修正回连地址才能恢复 Sandbox。禁止同时运行两个共享此数据库的 Gateway。