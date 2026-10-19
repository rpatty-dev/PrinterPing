"""
PrinterPing CLI - Instant Terminal Supply Health & Diagnostic Tool
"""

import sys
import os

# Import query functions from printerping
from printerping import query_printer_all, load_config, load_state, PRINTER_IP, PRINTER_NAME

def draw_bar(percent, width=15):
    if percent is None or percent < 0:
        return "[    N/A    ]"
    filled = max(0, min(width, int(width * (percent / 100.0))))
    empty = width - filled
    return f"[{'#' * filled}{'-' * empty}] {percent:3d}%"

def main():
    os.system("") # Enable ANSI colors
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    print(f"\n{BOLD}{CYAN}======================================================================{RESET}")
    print(f"{BOLD}  [🖨️] PrinterPing — Network Printer Health & Supply Monitor{RESET}")
    print(f"  Target: {PRINTER_NAME} ({PRINTER_IP})")
    print(f"{BOLD}{CYAN}======================================================================{RESET}\n")

    supplies, errors = query_printer_all()
    if not supplies:
        print(f"{RED}Error: Could not communicate with printer at {PRINTER_IP} via SNMP.{RESET}\n")
        sys.exit(1)

    state = load_state()
    spares = state.get("spares", {})

    if errors:
        print(f"{BOLD}{RED}>>> HARDWARE WARNING: {', '.join(errors)} <<<{RESET}\n")
    else:
        print(f"{GREEN}✓ Printer Hardware Status: Online & Healthy{RESET}\n")

    print(f"{BOLD}{'SUPPLY ITEM':<20} {'LEVEL':<24} {'STATUS'}{RESET}")
    print("-" * 65)

    for item, lvl in supplies.items():
        if isinstance(lvl, int) and lvl >= 0:
            bar = draw_bar(lvl, width=12)
            has_sp = spares.get(item, False)
            if lvl <= 5:
                color = RED
                status = "CRITICAL (REPLACE NOW)" if has_sp else "CRITICAL (REPLACE/ORDER)"
            elif lvl <= 15:
                color = CYAN if has_sp else YELLOW
                status = "LOW (SPARE ON HAND)" if has_sp else "LOW (ORDER REQUIRED)"
            else:
                color = GREEN
                status = "HEALTHY"
            print(f"{item:<20} {bar:<24} {color}{BOLD}{status}{RESET}")
        else:
            status_txt = "Healthy / OK" if lvl == -3 or lvl == 100 else "Check Required"
            print(f"{item:<20} {'[--- OK ---]':<24} {GREEN}{status_txt}{RESET}")

    print("=" * 65)
    print(f"\n{CYAN}Tip: Run `python printerping.py` to trigger background alert verification.{RESET}\n")

if __name__ == "__main__":
    main()
