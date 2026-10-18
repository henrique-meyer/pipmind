# Local/CI SQL Server only. Hosted state uses Azure SQL; never deploy this image there.
FROM mcr.microsoft.com/mssql/server:2022-latest@sha256:cf8f0ab2ebe61feec2eed2629061a07d247f791c2a8ddda74a4ac9f06a291718
USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/pipmind-microsoft.gpg \
    && echo 'deb [arch=amd64 signed-by=/usr/share/keyrings/pipmind-microsoft.gpg] https://packages.microsoft.com/ubuntu/22.04/mssql-server-2022 jammy main' > /etc/apt/sources.list.d/pipmind-mssql.list \
    && apt-get update && apt-get download mssql-server-fts=16.0.4305.5-1 \
    && echo '1299890caaa62fdc5334d65f8723f5457e6e154f8a02a2da78c7efd4c7c50336  mssql-server-fts_16.0.4305.5-1_amd64.deb' | sha256sum -c - \
    && dpkg-deb -x mssql-server-fts_16.0.4305.5-1_amd64.deb / \
    && rm mssql-server-fts_16.0.4305.5-1_amd64.deb && rm -rf /var/lib/apt/lists/*
USER mssql
