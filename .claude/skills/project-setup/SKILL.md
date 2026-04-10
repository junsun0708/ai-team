---
name: project-setup
description: "새 프로젝트 폴더를 생성하고 초기 설정(git init, .gitignore, 기본 구조)을 수행하는 스킬. '프로젝트 만들어', '폴더 만들고', '새로 시작', 'XX 프로젝트 생성' 등 프로젝트 초기화 요청 시 사용."
---

# Project Setup

~/a-projects/ 하위에 새 프로젝트를 생성하고 초기 설정을 수행한다.

## 필수 단계

1. `~/a-projects/{프로젝트명}/` 디렉토리 생성
2. `git init` 실행
3. `.gitignore` 생성 (아래 필수 항목 포함)
4. 프로젝트 유형에 맞는 기본 구조 생성

## .gitignore 필수 항목

```
.env
.venv/
node_modules/
__pycache__/
*.pyc
.DS_Store
dist/
build/
*.egg-info/
logs/
_workspace/
```

## 프로젝트 유형별 기본 구조

### Python 프로젝트
```
{name}/
├── .gitignore
├── requirements.txt
├── README.md
├── .env.example (필요 시)
└── {main_module}/
    └── __init__.py
```

### Node.js 프로젝트
```
{name}/
├── .gitignore
├── package.json
├── README.md
└── src/
    └── index.js
```

### 범용 프로젝트
```
{name}/
├── .gitignore
├── README.md
└── src/
```

## 보안 규칙
- `.env` 파일에 실제 키를 넣지 않는다.
- `.env.example`에 키 이름만 기록한다 (값은 비움).
- 시크릿이 포함된 파일은 `.gitignore`에 반드시 포함한다.
