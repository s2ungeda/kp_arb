# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 스펙 — 배포판 빌드 (build_exe.bat 에서 사용).
# 한 폴더(dist/meme)에 exe 2개: meme.exe(GUI, 콘솔 없음) + meme-core.exe(코어, 콘솔).
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

hidden = (
    collect_submodules("kp_arb")          # 지연 import(게이트웨이 등) 포함
    + collect_submodules("keyring")       # Windows 자격증명관리자 백엔드
    + collect_submodules("hyperliquid")   # HL SDK (bootstrap_live에서 지연 import)
)

# 32비트용 eth-account<0.11 계열(py_ecc·eth-*)은 import 때 importlib.metadata.version("…")으로 자기
# 버전을 읽는다 — 배포판에 dist-info가 없으면 PackageNotFoundError로 HL 게이트웨이 시동 실패
# (운영 PC 실측 2026-09-29 08:34). 메타데이터를 같이 싣는다.
metadata = []
for _pkg in ("py_ecc", "eth-abi", "eth-hash", "eth-keyfile", "eth-keys", "eth-rlp",
             "eth-typing", "eth-utils", "eth-account", "hexbytes"):
    try:
        metadata += copy_metadata(_pkg)
    except Exception:  # noqa: BLE001 - 설치 안 된 이름은 건너뜀
        pass

a = Analysis(
    ["kp_arb/app.py"],
    pathex=["."],
    datas=[("config.yaml", ".")] + metadata,  # 취급 종목 설정 — exe 옆에 배치 + 패키지 메타데이터
    hiddenimports=hidden,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe_gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="meme",
    console=False,   # 화면용 — cmd 창 없음
)
exe_core = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="meme-core",
    console=True,    # 코어 — 로그 확인용 콘솔
)
coll = COLLECT(exe_gui, exe_core, a.binaries, a.datas, name="meme")
