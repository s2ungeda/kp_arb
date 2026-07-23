"""키 등록 창 — Windows 자격증명관리자(keyring)에 비밀 저장. 평문 파일 불필요.

    keys.bat                (개발 PC)
    kp-arb.exe keys         (배포판 — Python 없이)

- 빈칸으로 두면 그 키는 **변경 없음** (이미 등록된 값 유지).
- 저장 후에도 창이 남아 등록 상태를 확인할 수 있다. 적용은 프로그램 재시작 후.
- 삭제는 개발 PC에서 `python -m kp_arb.secrets_cli del NAME`.
"""
from __future__ import annotations

from .config import KEYRING_SERVICE, SECRET_NAMES, KeyringSecrets


def main() -> None:
    """키 등록 창 실행 — 독립 프로그램(코어·메인과 무관)."""
    import tkinter as tk

    try:
        import keyring
    except ImportError:
        raise SystemExit("keyring 모듈이 없어 키 등록을 할 수 없습니다") from None

    root = tk.Tk()
    root.title("kp-arb 키 등록 (자격증명관리자)")
    root.resizable(False, False)
    root.option_add("*Font", ("Malgun Gothic", 9))

    provider = KeyringSecrets()
    tk.Label(root, text="빈칸 = 변경 없음. 저장 후 프로그램을 재시작해야 적용됩니다.",
             anchor="w", fg="gray25").grid(
        row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 4))

    # 실행 모드 — .env의 KP_MODE가 있으면 그게 우선(개발 PC), 없으면 이 값 사용
    import os
    mode_row = tk.Frame(root)
    mode_row.grid(row=len(SECRET_NAMES) + 1, column=0, columnspan=3,
                  sticky="w", padx=8, pady=(6, 0))
    tk.Label(mode_row, text="실행 모드").pack(side="left", padx=(0, 8))
    mode_var = tk.StringVar(value=provider.get("KP_MODE") or "paper")
    tk.Radiobutton(mode_row, text="모의(paper)", variable=mode_var,
                   value="paper").pack(side="left")
    tk.Radiobutton(mode_row, text="운영(live) — 실계좌 주문 주의", variable=mode_var,
                   value="live", fg="#8b0000").pack(side="left", padx=(8, 0))
    if os.environ.get("KP_MODE"):
        tk.Label(mode_row, text=f"(.env KP_MODE={os.environ['KP_MODE']} 가 우선)",
                 fg="gray40").pack(side="left", padx=(8, 0))

    entries: dict[str, tk.Entry] = {}
    status_labels: dict[str, tk.Label] = {}
    for i, (name, label) in enumerate(SECRET_NAMES, start=1):
        tk.Label(root, text=f"{label}  ({name})", anchor="w").grid(
            row=i, column=0, sticky="w", padx=8, pady=2)
        entry = tk.Entry(root, width=46, show="*")
        entry.grid(row=i, column=1, padx=4, pady=2)
        entries[name] = entry
        registered = provider.get(name) is not None
        status = tk.Label(root, text="등록됨" if registered else "없음",
                          fg="dark green" if registered else "#8b0000", width=6)
        status.grid(row=i, column=2, padx=(2, 8))
        status_labels[name] = status

    bottom = tk.Frame(root)
    bottom.grid(row=len(SECRET_NAMES) + 2, column=0, columnspan=3, pady=(6, 8))
    show_var = tk.BooleanVar(value=False)

    def toggle_show() -> None:
        mask = "" if show_var.get() else "*"
        for entry in entries.values():
            entry.config(show=mask)

    tk.Checkbutton(bottom, text="입력 표시", variable=show_var,
                   command=toggle_show).pack(side="left", padx=8)
    result = tk.Label(bottom, text="", fg="dark green")
    result.pack(side="left", padx=8)

    def save() -> None:
        saved = 0
        for name, entry in entries.items():
            value = entry.get().strip()
            if not value:
                continue  # 빈칸 = 변경 없음
            keyring.set_password(KEYRING_SERVICE, name, value)
            entry.delete(0, "end")
            status_labels[name].config(text="등록됨", fg="dark green")
            saved += 1
        keyring.set_password(KEYRING_SERVICE, "KP_MODE", mode_var.get())
        result.config(text=f"키 {saved}건 + 모드({mode_var.get()}) 저장 — 재시작 후 적용")

    tk.Button(bottom, text="저장", width=10, command=save).pack(side="left", padx=4)
    tk.Button(bottom, text="닫기", width=10, command=root.destroy).pack(side="left", padx=4)

    root.mainloop()


if __name__ == "__main__":
    main()
