---
name: review_test_quality
description: 评估测试覆盖与用例质量,覆盖未覆盖关键路径、弱断言、测试独立性缺失等。Invoke when 审查仓库的测试完备性,或用户关注"测试覆盖/边界未覆盖/单测缺失"时。
---

# 测试覆盖与用例质量审查

## 适用场景

用户要求评估测试覆盖度、单测缺失、边界未覆盖时执行。
支持形态:Python(pytest/unittest)、Node.js(vitest/jest)。其他语言按静态分析降级执行。

## 执行步骤

### 1. 识别测试框架与测试目录

```
find_files(pattern="**/test_*.py")          # pytest 风格
find_files(pattern="**/*_test.py")
find_files(pattern="**/*.test.{js,ts}")     # vitest/jest 风格
find_files(pattern="**/*.spec.{js,ts}")
read_file(file_path="package.json")         # 看 scripts.test / devDependencies
find_files(pattern="**/pytest.ini")         # pytest 配置
find_files(pattern="**/pyproject.toml")     # [tool.pytest] 配置
```

确认:测试目录结构、测试文件数量、有无 CI 跑测试的配置(.github/workflows 里的 test 步骤)。

### 2. 获取覆盖率(优先工具,逐级降级)

1. **首选** `run_coverage(repo_path)` — 返回总覆盖率与未覆盖文件清单
2. 工具不可用(返回 note)时降级 `run_command`:
   - Python: `pytest --cov --cov-report=term-missing -q`
   - vitest: `npx vitest run --coverage`
3. 测试跑不起来(缺依赖/环境)时降级为**静态评估**:跳过覆盖率数字,直接做第 3、4 步

拿到覆盖率后记录:总覆盖率、未覆盖最多的 top10 文件。

### 3. 交叉对比:未覆盖的是不是关键代码

对未覆盖 top 文件逐个判断(`read_file`):
- 是核心业务逻辑(支付/权限/数据写入)还是边角(日志/调试/CLI 壳)?
- 核心模块覆盖率 < 60% → high;边角代码不苛求
- 完全没有测试目录,但有实质业务代码 → 直接报 high:"无任何测试"

### 4. 抽查用例质量(抽样 5-10 个测试文件)

`read_file` 抽查,逐条对照:

**弱断言(最常见):**
- `assert result` 只判真假,不判具体值
- `assert response.status_code == 200` 但不验证响应体内容
- 捕获了异常却不断言异常类型(`with pytest.raises(Exception)` 太宽)

**边界缺失:**
- 只测了 happy path:空输入/空列表/None/超长字符串/负数/并发 一个都没测
- 数值函数没测 0 与边界值

**测试独立性:**
- 依赖执行顺序(测试间共享可变全局状态)
- 依赖外部服务却不 mock(真实网络/真实数据库无 fixture)
- 测试里 sleep 等待时序

**测试造假:**
- 断言值直接抄实现(测试与实现同源错误)
- mock 掉了被测对象本身,实际什么都没测

### 5. 输出结果

总结按此结构给出:
- **覆盖概况**:框架、测试文件数、总覆盖率(若可测)、未覆盖 top10
- **关键缺口**:未覆盖的核心模块列表(文件 + 为什么关键),严重度 high/medium
- **用例质量问题**:弱断言/边界缺失/独立性/造假各列实例(文件:行号)
- **建议**:按优先级给补测清单(先补哪个模块、补什么用例)

## 避免误报

- 覆盖率数字不是唯一标准:90% 覆盖率全是弱断言,不如 60% 覆盖关键路径+强断言
- 纯配置/生成代码/迁移脚本不必苛求覆盖
- 探索性脚本、examples 目录无测试不算问题

## 避免漏报

- 有测试 ≠ 测得对:务必抽查断言质量,别只看测试文件数量
- 注意"只测 mock 不测集成"的仓库:mock 层层嵌套后真实路径零覆盖
- 失败路径的测试最容易缺:异常分支、超时、重试耗尽
