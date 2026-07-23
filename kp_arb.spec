# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 스펙 — 배포판 빌드 (build_exe.bat 에서 사용).
# 한 폴더(dist/kp-arb)에 exe 2개: kp-arb.exe(GUI, 콘솔 없음) + kp-arb-core.exe(코어, 콘솔).
from PyInstaller.utils.hooks import collect_submodules

hidden = (
    collect_submodules("kp_arb")          # 지연 import(게이트웨이 등) 포함
    + collect_submodules("keyring")       # Windows 자격증명관리자 백엔드
    + collect_submodules("hyperliquid")   # HL SDK (bootstrap_live에서 지연 import)
)

a = Analysis(
    ["kp_arb/app.py"],
    pathex=["."],
    datas=[("config.yaml", ".")],  # 취급 종목 설정 — exe 옆에 배치
    hiddenimports=hidden,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe_gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="kp-arb",
    console=False,   # 화면용 — cmd 창 없음
)
exe_core = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="kp-arb-core",
    console=True,    # 코어 — 로그 확인용 콘솔
)
coll = COLLECT(exe_gui, exe_core, a.binaries, a.datas, name="kp-arb")
