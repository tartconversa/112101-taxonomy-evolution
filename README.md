# q010-taxonomy-evolution

治理分类目录版本、审核事件及复核记录的合成来源资料。

原始资料使用 UTF-8 CSV 保存，时间值采用带时区的 ISO 8601 格式。资料为虚构样例，其中包含不同事件时间、记录时间和待核实状态，不对应真实个人或平台。

## 目录

- `sources/` 保存原始业务记录。
- `sources/taxonomy.csv`
- `sources/content_events.csv`
- `sources/classifications.csv`
- `src/governance_taxonomy/` 是 Python 包入口。
- `tests/` 检查随项目提供的源文件格式。

## 环境

项目仅使用 Python 标准库，无需访问网络或安装第三方包。

## 运行基线检查

```powershell
python -m unittest discover -s tests -v
```
