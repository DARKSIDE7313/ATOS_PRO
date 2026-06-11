#!/usr/bin/env python3
"""
ATOS Model Orchestrator v2 — 多模型协同系统（优化版）
- 并行调用（审查/辩论阶段 Grok+Qwen 同时跑）
- 自动重试（网络波动不崩）
- 代码自动保存到文件
- 智能 token 限制（不浪费钱）
- 配置化（改模型不用改代码）

用法:
  python3 model_orchestrator.py code "写一个均值回归策略"
  python3 model_orchestrator.py review /path/to/code.py
  python3 model_orchestrator.py debate "这个策略有什么风险？"
  python3 model_orchestrator.py analyze /path/to/data.csv
"""

import urllib.request
import json
import sys
import os
import time
import argparse
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# CONFIG — 改这里调模型
# ============================================================
CONFIG = {
    'writer': {
        'id': 'x-ai/grok-4.3',
        'name': 'Grok 4.3',
        'input_price': 1.25,
        'output_price': 2.50,
    },
    'reviewer': {
        'id': 'qwen/qwen3.7-max',
        'name': 'Qwen3.7 Max',
        'input_price': 1.25,
        'output_price': 3.75,
    },
    'tester': {
        'id': 'deepseek/deepseek-v4-pro',
        'name': 'DeepSeek V4 Pro',
        'input_price': 0.435,
        'output_price': 0.87,
    },
    'synthesizer': {
        'id': 'x-ai/grok-4.3',
        'name': 'Grok 4.3',
        'input_price': 1.25,
        'output_price': 2.50,
    },
}

MAX_RETRIES = 3
RETRY_DELAY = 2
OUTPUT_DIR = os.path.expanduser('~/ATOS_PRO/generated')

# ============================================================
# API LAYER
# ============================================================

def _load_key() -> str:
    """Read OpenRouter API key once and cache it."""
    env_path = os.path.expanduser('~/.hermes/.env')
    try:
        with open(env_path) as f:
            for line in f:
                s = line.strip()
                if 'OPENROUTER_API_KEY' in s and '#' not in s and '=' in s:
                    return s.split('=', 1)[1]
    except Exception as e:
        print(f'[FATAL] 无法读取 API key: {e}')
        sys.exit(1)
    print('[FATAL] OPENROUTER_API_KEY 未在 .env 中找到')
    sys.exit(1)

_API_KEY = None

def _get_key():
    global _API_KEY
    if _API_KEY is None:
        _API_KEY = _load_key()
    return _API_KEY

def _extract_code(raw: str) -> str:
    """Extract Python code from model response, handling various markdown formats."""
    # Try ```python ... ```
    m = re.search(r'```(?:python)?\s*\n?(.*?)```', raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Try single backtick blocks
    m = re.search(r'`{3}(?:py)?\n?(.*?)`{3}', raw, re.DOTALL)
    if m:
        return m.group(1).strip()
    # No code block — return raw (might just be code)
    return raw.strip()


def call_model(role: str, system: str, prompt: str,
               max_tokens: int = 2000, temp: float = 0.1) -> dict:
    """Call a model with retry logic. Returns dict with text/tokens/time."""
    cfg = CONFIG[role]
    model_id = cfg['id']
    key = _get_key()
    
    last_error = None
    for attempt in range(MAX_RETRIES):
        start = time.time()
        try:
            messages = [{'role': 'user', 'content': prompt}]
            if system:
                messages.insert(0, {'role': 'system', 'content': system})
            
            data = json.dumps({
                'model': model_id,
                'messages': messages,
                'max_tokens': max_tokens,
                'temperature': temp,
            }).encode()
            
            req = urllib.request.Request(
                'https://openrouter.ai/api/v1/chat/completions',
                data=data,
                headers={
                    'Authorization': f'Bearer {key}',
                    'Content-Type': 'application/json',
                },
                method='POST'
            )
            
            resp = urllib.request.urlopen(req, timeout=90)
            d = json.loads(resp.read())
            elapsed = round(time.time() - start, 1)
            
            usage = d.get('usage', {})
            in_tok = usage.get('prompt_tokens', 0)
            out_tok = usage.get('completion_tokens', 0)
            
            cost = (in_tok / 1_000_000 * cfg['input_price']) + \
                   (out_tok / 1_000_000 * cfg['output_price'])
            
            return {
                'text': d['choices'][0]['message']['content'],
                'input_tokens': in_tok,
                'output_tokens': out_tok,
                'cost': round(cost, 6),
                'time': elapsed,
                'role': role,
                'model': cfg['name'],
            }
            
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            last_error = f'HTTP {e.code}: {body}'
            if e.code in (429, 503):
                wait = RETRY_DELAY * (attempt + 1)
                print(f'  [重试 {attempt+1}/{MAX_RETRIES}] {cfg["name"]} 限流，等待{wait}s...')
                time.sleep(wait)
                continue
            break
        except Exception as e:
            last_error = str(e)
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_DELAY * (attempt + 1)
                print(f'  [重试 {attempt+1}/{MAX_RETRIES}] {cfg["name"]} 错误: {str(e)[:60]}')
                time.sleep(wait)
                continue
            break
    
    return {
        'text': f'[ERROR] {last_error}',
        'error': last_error,
        'role': role,
        'model': cfg['name'],
    }

def save_output(filename: str, content: str) -> str:
    """Save generated file and return path."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(OUTPUT_DIR, f'{ts}_{filename}')
    with open(path, 'w') as f:
        f.write(content)
    print(f'  [保存] {path}')
    return path

def report(result: dict):
    """Print one-line cost report for a model call."""
    name = result.get('model', '?')
    if 'error' in result:
        print(f'  [{name}] FAILED: {result["error"]}')
        return
    print(f'  [{name}] {result["input_tokens"]}→{result["output_tokens"]} tok, '
          f'${result["cost"]:.5f}, {result["time"]}s')

# ============================================================
# MODES
# ============================================================

def mode_code(instruction: str) -> None:
    """
    写代码流水线（优化版）
    Phase 1: Grok 写代码
    Phase 2: Qwen 审查（单独跑）
    Phase 3: Grok 修复
    Phase 4: DeepSeek 写测试
    """
    print(f'\n{"="*55}')
    print(f'  写代码模式')
    print(f'  指令: {instruction[:80]}')
    print(f'{"="*55}')

    total_cost = 0.0
    total_time = 0.0

    # Phase 1: Write
    print('\n  1/4 编写代码...')
    sys.stdout.flush()
    w = call_model('writer',
        'You are a senior quant dev. Write production Python with type hints, '
        'docstrings, error handling. Code only.',
        f'Write Python code for:\n{instruction}\n\nInclude type hints, docstrings, input validation.',
        max_tokens=3000)
    report(w)
    if 'error' in w:
        return
    total_cost += w['cost']
    total_time += w['time']
    
    code = _extract_code(w['text'])
    print(f'     {len(code.splitlines())} 行代码')

    # Phase 2: Review
    print('\n  2/4 代码审查...')
    sys.stdout.flush()
    r = call_model('reviewer',
        'You are a ruthless quant fund code reviewer. Find ALL bugs. '
        'Rate 1-10. Be specific: line, severity (CRITICAL/MAJOR/MINOR), fix.',
        f'Review this code:\n\n```python\n{code}\n```\n\n'
        f'List each issue with: line, severity, explanation, fix.',
        max_tokens=2500)
    report(r)
    if 'error' in r:
        return
    total_cost += r['cost']
    total_time += r['time']
    
    review = r['text']
    criticals = review.lower().count('critical')
    majors = review.lower().count('major') - criticals
    print(f'     发现 {criticals} CRITICAL, {majors} MAJOR 问题')

    # Phase 3: Fix
    print('\n  3/4 修复问题...')
    sys.stdout.flush()
    f = call_model('writer',
        'You are a senior engineer. Output COMPLETE fixed code, not just diffs.',
        f'Original code:\n```python\n{code}\n```\n\n'
        f'Review (fix ALL):\n{review}\n\nOutput COMPLETE fixed code.',
        max_tokens=3000)
    report(f)
    if 'error' in f:
        return
    total_cost += f['cost']
    total_time += f['time']
    
    fixed = _extract_code(f['text'])
    final_path = save_output('code.py', fixed)

    # Phase 4: Tests (with timeout fallback)
    print('\n  4/4 生成测试...')
    sys.stdout.flush()
    try:
        t = call_model('tester',
            'You are a QA engineer. Write pytest tests covering normal, edge, error cases.',
            f'Write tests for:\n```python\n{fixed}\n```\n\n'
            f'Include: test_normal, test_edge_cases, test_invalid_input.',
            max_tokens=2000)
        report(t)
        if 'error' not in t:
            total_cost += t['cost']
            total_time += t['time']
            save_output('test_code.py', t['text'])
        else:
            print('  [跳过] 测试生成失败，代码已保存可直接运行')
    except Exception as e:
        print(f'  [跳过] 测试生成超时: {str(e)[:50]}')

    # Summary
    print(f'\n{"="*55}')
    print(f'  完成')
    print(f'  耗时: {total_time:.1f}s | 费用: ${total_cost:.5f}')
    print(f'  文件: {final_path}')
    print(f'{"="*55}')
    
    # Print final code
    print(f'\n--- 最终代码 ({len(fixed.splitlines())} 行) ---')
    print(fixed)


def mode_review(file_path: str) -> None:
    """
    双模型并行审查（优化版）
    Grok + Qwen 同时审 → 汇总唯一 bugs
    """
    if not os.path.exists(file_path):
        print(f'文件不存在: {file_path}')
        return
    
    with open(file_path) as f:
        code = f.read()
    
    lines = len(code.splitlines())
    print(f'\n{"="*55}')
    print(f'  双模型并行审查')
    print(f'  文件: {file_path} ({lines} 行)')
    print(f'{"="*55}')

    total_cost = 0.0

    # Parallel: Grok + Qwen review simultaneously
    system = 'You are a senior code reviewer. Find ALL bugs. Rate 1-10. Be specific.'
    prompt = f'Review:\n\n```python\n{code}\n```\n\nList: line, severity, fix.'
    
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(call_model, 'writer', system, prompt, 2500)
        f2 = pool.submit(call_model, 'reviewer', system, prompt, 2500)
        
        for f in as_completed([f1, f2]):
            r = f.result()
            report(r)
            if 'error' not in r:
                total_cost += r['cost']
    
    print(f'\n总费用: ${total_cost:.5f}')


def mode_debate(question: str) -> None:
    """
    多模型辩论（优化版）
    Grok + Qwen + DeepSeek 同时回答 → Grok 综合
    """
    print(f'\n{"="*55}')
    print(f'  多模型辩论')
    print(f'  问题: {question[:80]}')
    print(f'{"="*55}')

    total_cost = 0.0
    answers = {}

    # Phase 1: All 3 models answer in parallel
    print('\n  三方独立分析...')
    sys.stdout.flush()
    
    system = 'You are a senior quant. Think step by step.'
    
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(call_model, role, system, question, 2000): role
            for role in ['writer', 'reviewer', 'tester']
        }
        for f in as_completed(futures):
            r = f.result()
            report(r)
            if 'error' not in r:
                total_cost += r['cost']
                answers[futures[f]] = r['text']

    if not answers:
        print('\n[ERROR] 所有模型都失败了')
        return

    # Phase 2: Synthesize
    print('\n  综合各方观点...')
    sys.stdout.flush()
    
    synthesis = f'Question: {question}\n\n'
    for role, text in answers.items():
        synthesis += f'=== {CONFIG[role]["name"]} ===\n{text}\n\n'
    synthesis += 'Synthesize the best answer. Resolve contradictions. Highlight consensus.'
    
    s = call_model('synthesizer',
        'You are a senior partner synthesizing your team analysis.',
        synthesis, max_tokens=2000)
    report(s)
    if 'error' not in s:
        total_cost += s['cost']
        final = s['text']
    else:
        # Fallback: use first answer
        final = list(answers.values())[0]

    print(f'\n{"="*55}')
    print(f'  辩论结论 (费用: ${total_cost:.5f})')
    print(f'{"="*55}')
    print(f'\n{final}')


def mode_analyze(data_path: str) -> None:
    """
    数据分析流水线（优化版）
    DeepSeek 处理 → Grok 解读 → Qwen 验证
    """
    if not os.path.exists(data_path):
        print(f'文件不存在: {data_path}')
        return

    with open(data_path) as f:
        head = ''.join([f.readline() for _ in range(30)])
    
    print(f'\n{"="*55}')
    print(f'  数据分析')
    print(f'  文件: {data_path}')
    print(f'{"="*55}')

    total_cost = 0.0
    preview = head[:800]

    # Phase 1: DeepSeek processes
    print('\n  1/3 数据处理...')
    sys.stdout.flush()
    d1 = call_model('tester',
        'You are a data engineer.',
        f'Preview:\n{preview}\n\n'
        f'Write pandas code to: compute stats, find anomalies, identify patterns. '
        f'Code only.',
        max_tokens=1500)
    report(d1)
    if 'error' in d1:
        return
    total_cost += d1['cost']
    analysis_code = d1['text']

    # Phase 2: Grok interprets (parallel with phase 3 if possible)
    print('\n  2/3 结果解读...')
    sys.stdout.flush()
    d2 = call_model('writer',
        'You are a quantitative analyst.',
        f'Preview:\n{preview}\n\n'
        f'Analysis code:\n{analysis_code}\n\n'
        f'Key insights? Patterns? Actionable for a trader?',
        max_tokens=1500)
    report(d2)
    if 'error' not in d2:
        total_cost += d2['cost']

    # Phase 3: Qwen validates
    print('\n  3/3 结论验证...')
    sys.stdout.flush()
    d3 = call_model('reviewer',
        'You are a skeptical quant. Challenge every conclusion.',
        f'Data:\n{preview}\n\n'
        f'Interpretation:\n{d2["text"]}\n\n'
        f'Critically evaluate. Any alternative explanations? Statistical pitfalls?',
        max_tokens=1500)
    report(d3)
    if 'error' not in d3:
        total_cost += d3['cost']

    # Output
    print(f'\n{"="*55}')
    print(f'  分析完成 (费用: ${total_cost:.5f})')
    print(f'{"="*55}')
    print(f'\n--- 关键洞察 ---\n{d2["text"]}')
    print(f'\n--- 验证与风险 ---\n{d3["text"]}')


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='ATOS Model Orchestrator v2 — 多模型协同系统',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    
    sub = parser.add_subparsers(dest='mode')
    
    p = sub.add_parser('code', help='写代码')
    p.add_argument('instruction', help='需求描述')
    
    p = sub.add_parser('review', help='代码审查')
    p.add_argument('file', help='文件路径')
    
    p = sub.add_parser('debate', help='多模型辩论')
    p.add_argument('question', help='问题')
    
    p = sub.add_parser('analyze', help='数据分析')
    p.add_argument('file', help='数据文件路径')
    
    args = parser.parse_args()
    
    if not args.mode:
        parser.print_help()
        return
    
    mode_map = {
        'code': lambda: mode_code(args.instruction),
        'review': lambda: mode_review(args.file),
        'debate': lambda: mode_debate(args.question),
        'analyze': lambda: mode_analyze(args.file),
    }
    mode_map[args.mode]()

if __name__ == '__main__':
    main()
