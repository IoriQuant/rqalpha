# encoding: utf-8
import toml

# 读取 poetry.lock 文件
with open('poetry.lock', 'r', encoding='utf-8') as f:
    lock_data = toml.load(f)

# 提取依赖信息
dependencies = []
for package in lock_data['package']:
    name = package['name']
    version = package['version']
    dependencies.append(f"{name}=={version}")

# 写入 requirements.txt 文件
with open('requirements.txt', 'w') as f:
    f.write('\n'.join(dependencies))