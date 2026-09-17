# 洛雪音源脚本订阅服务。
# 基础镜像可用 --build-arg BASE_IMAGE=... 覆盖（与根项目 ensure_base_image.sh 一致）。
ARG BASE_IMAGE=node:22-alpine
FROM ${BASE_IMAGE}

ENV NODE_ENV=production \
    LXSOURCE_PORT=8774 \
    LXSOURCE_HOST=0.0.0.0

WORKDIR /app

# 零 npm 依赖：只用 Node 内置模块（node:vm / node:http / 内置 fetch）。
# 仅复制运行时文件，测试与文档不进入镜像。
COPY --chown=node:node shim.js subscription.js server.js ./
RUN chmod 0644 /app/*.js

USER node

EXPOSE 8774

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD node -e "fetch('http://127.0.0.1:'+(process.env.LXSOURCE_PORT||8774)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
