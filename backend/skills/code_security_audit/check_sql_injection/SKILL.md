---
name: check_sql_injection
description: 检查 SQL 注入漏洞,覆盖拼接、ORM raw 查询、动态表名等模式。Invoke when审计涉及数据库操作的代码,特别是 Python/Node/Java 的 SQL 拼接点。
---

# SQL 注入审计

## 适用场景

仓库里有任何数据库交互代码(SQLite/MySQL/PostgreSQL/Oracle 等)都应执行本技能。
重点语言:Python、Node.js(JavaScript/TypeScript)、Java、PHP、Go。

## 执行步骤

### 1. 定位数据库交互点

调用 `search_code` 搜以下高危模式(每次搜一个,避免正则太宽泛):

**Python(Django / SQLAlchemy / sqlite3 / 原生 DB-API):**
```
search_code(pattern="execute\\s*\\(|executemany\\s*\\(|cursor\\.execute", file_glob="*.py")
search_code(pattern="\\.raw\\s*\\(|\\.extra\\s*\\(.*where", file_glob="*.py")
search_code(pattern="text\\s*\\(['\"]", file_glob="*.py")  # SQLAlchemy text() 拼接
```

**Node.js(mysql / mysql2 / pg / sequelize / knex):**
```
search_code(pattern="\\.query\\s*\\(", file_glob="*.js")
search_code(pattern="\\.query\\s*\\(", file_glob="*.ts")
search_code(pattern="knex\\.raw\\s*\\(", file_glob="*.js")
```

**Java(JDBC / MyBatis / JPA):**
```
search_code(pattern="createStatement\\s*\\(|prepareStatement", file_glob="*.java")
search_code(pattern="Statement\\.executeQuery|executeUpdate", file_glob="*.java")
```

**PHP(mysqli / PDO):**
```
search_code(pattern="mysqli_query\\s*\\(|->query\\s*\\(", file_glob="*.php")
```

### 2. 判断是否用户可控

对每个搜索命中点,调用 `read_file` 看上下文(±20 行):

- 参数是否来自用户输入(request / req / args / params / $_GET / $_POST / @PathVariable)
- 是否用了**参数化查询**(`?` 占位符、`%s` 占位符、命名参数 `:name`、prepared statement)
- 是否拼接了字符串(`+`、`%`、`f-string`、`format()`、模板字符串 `${}`)

**安全的特征:**
- 用 `cursor.execute("SELECT ... WHERE id = ?", (user_id,))` 形式
- ORM 的 `.filter(Model.field == value)` 链式调用

**危险的特征:**
- `cursor.execute(f"SELECT ... WHERE id = {user_id}")`
- `cursor.execute("SELECT ... WHERE id = " + user_id)`
- `cursor.execute("SELECT ... WHERE id = %s" % user_id)`

### 3. 验证(可选,沙箱可用时)

若你判断有 SQL 注入,可构造最小验证脚本(若沙箱支持运行自定义脚本):

```python
# 仅当目标仓库有可启动的 web 服务时,才能跑验证。
# 否则仅基于代码静态分析判定即可,不要强行跑沙箱。
```

### 4. 提交结果

调用 `submit_results` 提交。每个 SQL 注入:

- `title`: `[high] CWE-89 SQL注入 <file>:<line>`
- `content`: 漏洞描述 + 用户可控路径 + 修复建议(改用参数化查询)
- `metadata`: `{"cwe": "CWE-89", "severity": "high", "file_path": "...", "line_range": "...", "remediation": "改用参数化查询"}`

若整个仓库无 SQL 注入,也要在最终 `submit_results` 的 summary 里写明:
"已检查 SQL 注入:搜到 N 个数据库交互点,均为参数化查询,无注入风险。"

## 避免误报

- 测试文件(`test_*.py` / `*_test.go` / `tests/`)里的拼接:标 `info`,不算生产漏洞
- 配置文件里的 SQL 是字符串常量,不是注入
- ORM 链式调用 `.filter()` 是安全的,不需要报告

## 避免漏报

- 不要只搜 `execute(` 一个模式,ORM 的 `.raw()`、`.extra()`、SQLAlchemy 的 `text()` 都是注入点
- Python 的 `%` 字符串格式化拼接 SQL 是最常见的注入形态
