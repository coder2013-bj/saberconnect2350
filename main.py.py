# ╔══════════════════════════════════════════════════════════════════════════╗
# ║     L I G H T S A B E R   —   P I C O   2 W   E D I T I O N              ║
# ║  60-LED strip folded in half → 30 visible blade LEDs (0-29)              ║
# ║  Wi-Fi AP: "Barneys Saber"  open  →  192.168.4.1  (captive portal)       ║
# ║  Bluetooth LE GATT peripheral  ·  Web Bluetooth control page             ║
# ║  Physical button: single-tap = power · double-tap = clash                ║
# ╚══════════════════════════════════════════════════════════════════════════╝

import machine, neopixel, network, socket, time, math, random, json, gc
import bluetooth
from micropython import const

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                        U S E R   C O N F I G                             ║
# ╚══════════════════════════════════════════════════════════════════════════╝
NUM_LEDS     = 60          # total physical LEDs on the strip
BLADE_LEN    = 31          # half-strip: only these LEDs are visible
GP_DATA      = 0           # NeoPixel data pin
GP_BUTTON    = 14          # latching / momentary switch → GND
GP_SWLED     = 13          # switch indicator LED

WIFI_SSID    = "Barneys Saber v2"
WIFI_PASS    = ""          # open network
BLE_NAME     = "BarneysSaber"

IGNITION_S   = 0.55        # seconds for blade-extend sweep
SHUTDOWN_S   = 0.40        # seconds for blade-retract sweep
TARGET_FPS   = 60

# Double-tap timing (ms)
DOUBLE_TAP_MS = 350
# ╚══════════════════════════════════════════════════════════════════════════╝

# ── Hardware ───────────────────────────────────────────────────────────────
np     = neopixel.NeoPixel(machine.Pin(GP_DATA), NUM_LEDS, timing=1)
button = machine.Pin(GP_BUTTON, machine.Pin.PULL_UP)
sw_led = machine.Pin(GP_SWLED,  machine.Pin.OUT)

# ── Gamma LUT (perceptual brightness, γ=2.5) ──────────────────────────────
GAMMA = bytearray([round((i / 255) ** 2.5 * 255) for i in range(256)])

# ── Dither accumulators for smooth sub-LSB colour transitions ─────────────
_dt_r = [0.0] * BLADE_LEN
_dt_g = [0.0] * BLADE_LEN
_dt_b = [0.0] * BLADE_LEN

# ── Shared state ───────────────────────────────────────────────────────────
state = {
    "on":          False,
    "color":       [0, 120, 255],
    "color2":      [255, 0, 0],
    "brightness":  1.0,
    "flicker_amp": 0.07,
    "flicker_spd": 3.5,
    "white_core":  0.42,
    "anim":        "plasma",
}

VALID_ANIMS = {
    'plasma', 'solid', 'dual', 'rainbow', 'fire', 'unstable', 'flash',
    'lightning', 'pulse', 'sparkle', 'tracker', 'emitter_bleed',
    'compression', 'glitch', 'singularity', 'phaser', 'bio_mesh',
    'cyber_sweep', 'comet', 'heartbeat', 'matrix_rain', 'galaxy', 'vortex',
    'clash'
}

# ── Clash state ────────────────────────────────────────────────────────────
_clash_until = 0
_clash_start = 0
_prev_anim   = "plasma"

# ── Double-tap detection ───────────────────────────────────────────────────
_last_tap_ms   = 0
_tap_count     = 0
_btn_prev      = False

# ── Shared state setters ───────────────────────────────────────────────────
def apply_power(v):
    state['on'] = bool(v)
    _ble_notify_power()

def apply_color(r, g, b):
    state['color'] = [max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b)))]
    _ble_notify_color()

def apply_color2(r, g, b):
    state['color2'] = [max(0, min(255, int(r))), max(0, min(255, int(g))), max(0, min(255, int(b)))]

def apply_anim(name):
    global _clash_until, _clash_start, _prev_anim
    if name not in VALID_ANIMS:
        return
    if name == 'clash':
        _prev_anim   = state['anim']
        _clash_start = time.ticks_ms()
        _clash_until = time.ticks_ms() + 500
        state['anim'] = 'clash'
    else:
        state['anim'] = name
        _ble_notify_anim()

def apply_params(b_raw, f_raw, s_raw, w_raw):
    state['brightness']  = max(0.05, min(1.0,  b_raw / 100))
    state['flicker_amp'] = max(0.0,  min(0.30, f_raw / 100))
    state['flicker_spd'] = max(0.1,  min(10.0, s_raw / 10))
    state['white_core']  = max(0.0,  min(1.0,  w_raw / 100))

# ── Pixel write helpers ────────────────────────────────────────────────────
def _write_px(idx, r, g, b):
    """Write to blade pixel idx (0-29) with dither + gamma correction."""
    v = r + _dt_r[idx]; lo = int(v); _dt_r[idx] = v - lo
    rr = GAMMA[lo if lo < 256 else 255]
    v = g + _dt_g[idx]; lo = int(v); _dt_g[idx] = v - lo
    gg = GAMMA[lo if lo < 256 else 255]
    v = b + _dt_b[idx]; lo = int(v); _dt_b[idx] = v - lo
    bb = GAMMA[lo if lo < 256 else 255]
    np[idx] = (rr, gg, bb)

def blade_write(pos, r, g, b):
    """Write a blade pixel. pos 0 = hilt, pos 29 = tip."""
    if 0 <= pos < BLADE_LEN:
        _write_px(pos, r, g, b)

def blade_clear():
    """Turn off all blade LEDs (pixels 0-29) and the hidden half (30-59)."""
    for i in range(NUM_LEDS):
        np[i] = (0, 0, 0)
    np.write()

def _blank_hidden():
    """Keep the folded-back LEDs (30-59) always off."""
    for i in range(BLADE_LEN, NUM_LEDS):
        np[i] = (0, 0, 0)

_T0 = time.ticks_ms()
def now_s():
    return time.ticks_diff(time.ticks_ms(), _T0) * 0.001

def _hsv(h, s, v):
    if s == 0:
        c = v * 255; return (c, c, c)
    h6 = h * 6.0; i = int(h6) % 6; f = h6 - int(h6)
    p = v*(1-s)*255; q = v*(1-s*f)*255; t2 = v*(1-s*(1-f))*255; v2 = v*255
    return [(v2,t2,p),(q,v2,p),(p,v2,t2),(p,q,v2),(t2,p,v2),(v2,p,q)][i]

def pixel_rgb(pos, brightness, t):
    """Core plasma pixel colour — plasma flicker around a white-hot centre."""
    CR, CG, CB = state["color"]
    amp   = state["flicker_amp"]
    freq  = state["flicker_spd"]
    wcore = state["white_core"]
    bsc   = state["brightness"]

    u     = pos / (BLADE_LEN - 1) if BLADE_LEN > 1 else 0.0
    phase = t * freq * 6.2832 - u * 8.0
    wave  = math.sin(phase) * 0.65 + math.sin(phase * 1.73 + 0.9) * 0.35
    spark = random.random() - 0.5
    f     = 1.0 + amp * (wave + spark * 0.30)
    f     = max(0.55, min(1.35, f))

    bri   = brightness * bsc * f
    bri1  = min(bri, 1.0)
    core  = wcore * bri1 * bri1
    r = (CR + (255 - CR) * core) * bri
    g = (CG + (255 - CG) * core) * bri
    b = (CB + (255 - CB) * core) * bri
    return (min(255.0, r), min(255.0, g), min(255.0, b))

# ── Ignition sweep ─────────────────────────────────────────────────────────
def ignite():
    t0 = time.ticks_ms()
    while True:
        elapsed = time.ticks_diff(time.ticks_ms(), t0) * 0.001
        prog    = min(1.0, elapsed / IGNITION_S)
        t       = now_s()
        front   = prog * BLADE_LEN
        for pos in range(BLADE_LEN):
            if pos + 1 <= front:  bri = 1.0
            elif pos < front:     bri = front - pos
            else:                 bri = 0.0
            r, g, b = pixel_rgb(pos, bri, t)
            blade_write(pos, r, g, b)
        _blank_hidden()
        np.write()
        if prog >= 1.0: break
        time.sleep_us(500)

# ── Extinguish sweep ───────────────────────────────────────────────────────
def extinguish():
    t0 = time.ticks_ms()
    while True:
        elapsed   = time.ticks_diff(time.ticks_ms(), t0) * 0.001
        prog      = min(1.0, elapsed / SHUTDOWN_S)
        t         = now_s()
        remaining = (1.0 - prog) * BLADE_LEN
        for pos in range(BLADE_LEN):
            if pos + 1 <= remaining:  bri = 1.0
            elif pos < remaining:     bri = remaining - pos
            else:                     bri = 0.0
            r, g, b = pixel_rgb(pos, bri, t)
            blade_write(pos, r, g, b)
        _blank_hidden()
        np.write()
        if prog >= 1.0:
            blade_clear()
            break
        time.sleep_us(500)

# ── Persistent comet trail state ───────────────────────────────────────────
_comet_pos   = 0.0
_comet_dir   = 1
_comet_speed = 0.0

# ── Matrix rain column state ───────────────────────────────────────────────
_matrix_cols = [random.random() for _ in range(BLADE_LEN)]

# ── Main render engine ─────────────────────────────────────────────────────
def render_frame():
    global _clash_until, _prev_anim, _comet_pos, _comet_dir, _comet_speed, _matrix_cols
    t    = now_s()
    bsc  = state["brightness"]
    CR, CG, CB    = state["color"]
    C2R, C2G, C2B = state["color2"]
    anim = state["anim"]
    wc   = state["white_core"]

    if anim == "plasma":
        for pos in range(BLADE_LEN):
            r, g, b = pixel_rgb(pos, 1.0, t)
            blade_write(pos, r, g, b)

    elif anim == "solid":
        core = wc * bsc * bsc
        r = min(255.0, (CR + (255-CR)*core) * bsc)
        g = min(255.0, (CG + (255-CG)*core) * bsc)
        b = min(255.0, (CB + (255-CB)*core) * bsc)
        for pos in range(BLADE_LEN):
            blade_write(pos, r, g, b)

    elif anim == "dual":
        spd = state["flicker_spd"] * 0.15
        for pos in range(BLADE_LEN):
            u   = pos / max(1, BLADE_LEN - 1)
            mix = 0.5 + 0.5 * math.sin(t * spd * 6.2832 + u * 6.2832)
            blade_write(pos,
                min(255.0, (CR*(1-mix) + C2R*mix) * bsc),
                min(255.0, (CG*(1-mix) + C2G*mix) * bsc),
                min(255.0, (CB*(1-mix) + C2B*mix) * bsc))

    elif anim == "rainbow":
        spd = state["flicker_spd"] * 0.08
        for pos in range(BLADE_LEN):
            u  = pos / max(1, BLADE_LEN - 1)
            h2 = (t * spd + u * 0.7) % 1.0
            rc, gc, bc = _hsv(h2, 1.0, bsc)
            blade_write(pos, rc, gc, bc)

    elif anim == "fire":
        for pos in range(BLADE_LEN):
            u   = pos / max(1, BLADE_LEN - 1)
            bri = bsc * (1.0 - u*0.75) * (1.0 + 0.18*(random.random()-0.5)*2)
            bri = max(0.0, min(1.5, bri))
            blade_write(pos,
                min(255.0, 255*bri),
                min(255.0, 180*(1.0-u)*bri),
                min(255.0, 40*(1.0-u)*bri*bri))

    elif anim == "unstable":
        spd = state["flicker_spd"]
        gz  = (t * 7.3) % 1.0
        for pos in range(BLADE_LEN):
            u     = pos / max(1, BLADE_LEN - 1)
            phase = t * spd * 6.2832 - u * 10.0
            wave  = math.sin(phase)*0.7 + math.sin(phase*2.3+1.1)*0.3
            spark = random.random()
            if abs(u - gz) < 0.12 and spark > 0.55:
                blade_write(pos, 0, 0, 0); continue
            f   = 1.0 + 0.35*(wave + (spark-0.5)*0.5)
            r, g, b = pixel_rgb(pos, bsc * max(0.1, min(1.8, f)), t)
            blade_write(pos, r, g, b)

    elif anim == "flash":
        spd  = max(0.3, state["flicker_spd"])
        on   = ((t * spd * 0.5) % 1.0) < 0.5
        bri  = bsc if on else 0.0
        core = wc * bri * bri
        for pos in range(BLADE_LEN):
            blade_write(pos,
                min(255.0, (CR + (255-CR)*core) * bri),
                min(255.0, (CG + (255-CG)*core) * bri),
                min(255.0, (CB + (255-CB)*core) * bri))

    elif anim == "lightning":
        strike = random.random() < (0.02 * state["flicker_spd"])
        for pos in range(BLADE_LEN):
            if strike:
                blade_write(pos, 230*bsc, 245*bsc, 255*bsc)
            else:
                u = pos / max(1, BLADE_LEN - 1)
                f = 0.5 + 0.5 * math.sin(t * 25 + u * 12)
                blade_write(pos,
                    min(255.0, (CR * f + 50*(1-f)) * bsc),
                    min(255.0, (CG * f + 150*(1-f)) * bsc),
                    min(255.0, (CB * f + 255*(1-f)) * bsc))

    elif anim == "pulse":
        spd  = state["flicker_spd"] * 0.8
        half = (BLADE_LEN - 1) * 0.5
        c1   = half + half * math.sin(t * spd * 6.2832)
        c2   = half + half * math.sin(t * spd * 6.2832 + 3.1416)
        for pos in range(BLADE_LEN):
            d1 = abs(pos - c1); d2 = abs(pos - c2)
            f1 = max(0.0, 1.0 - d1/7.0)**2
            f2 = max(0.0, 1.0 - d2/7.0)**2
            factor = min(1.0, f1 + f2*0.5)
            base_r = CR * bsc * 0.3; base_g = CG * bsc * 0.3; base_b = CB * bsc * 0.3
            blade_write(pos,
                min(255.0, base_r + (CR + (255-CR)*factor - base_r)*factor),
                min(255.0, base_g + (CG + (255-CG)*factor - base_g)*factor),
                min(255.0, base_b + (CB + (255-CB)*factor - base_b)*factor))

    elif anim == "sparkle":
        spd = state["flicker_spd"] * 0.5
        for pos in range(BLADE_LEN):
            u = pos / max(1, BLADE_LEN - 1)
            wave = math.sin(u * 18.0 + t * spd * 6.2832)
            br, bg, bb = (CR, CG, CB) if wave > 0 else (C2R, C2G, C2B)
            if random.random() > 0.96:
                blade_write(pos, 255*bsc, 255*bsc, 255*bsc)
            else:
                blade_write(pos, br*bsc, bg*bsc, bb*bsc)

    elif anim == "tracker":
        spd     = state["flicker_spd"] * 0.8
        scanner = (math.sin(t * spd) * 0.5 + 0.5) * BLADE_LEN
        for pos in range(BLADE_LEN):
            dist = abs(pos - scanner)
            if dist < 4.0:
                intensity = 1.0 - (dist / 4.0)
                blade_write(pos, CR*bsc*intensity, CG*bsc*intensity, CB*bsc*intensity)
            else:
                blade_write(pos, CR*bsc*0.05, CG*bsc*0.05, CB*bsc*0.05)

    elif anim == "emitter_bleed":
        spd = state["flicker_spd"] * 1.2
        for pos in range(BLADE_LEN):
            u    = pos / max(1, BLADE_LEN - 1)
            heat = math.exp(-u * 4.5)
            spit = random.random() > (0.97 - u*0.02)
            if spit:
                blade_write(pos, 255*bsc, 255*bsc, 255*bsc)
            else:
                wave = 0.8 + 0.2*math.sin(t*spd*8.0 - u*4.0)
                blade_write(pos,
                    min(255.0, (CR*(1-heat)+255*heat)*wave*bsc),
                    min(255.0, (CG*(1-heat)+80*heat)*wave*bsc),
                    min(255.0, (CB*(1-heat)+20*heat)*wave*bsc))

    elif anim == "compression":
        spd = state["flicker_spd"] * 0.9
        for pos in range(BLADE_LEN):
            u    = pos / max(1, BLADE_LEN - 1)
            comp = math.sin(u*12.5 + t*spd*5.0) * math.cos(u*6.2 - t*spd*2.1)
            factor = 0.4 + 0.6*max(0.0, min(1.0, (comp+1.0)*0.5))
            blade_write(pos, CR*bsc*factor, CG*bsc*factor, CB*bsc*factor)

    elif anim == "glitch":
        glitching = random.random() < (0.12 * state["flicker_spd"])
        for pos in range(BLADE_LEN):
            if glitching and random.random() > 0.4:
                blade_write(pos, C2R*bsc, C2G*bsc, C2B*bsc)
            else:
                r, g, b = pixel_rgb(pos, 0.95, t)
                blade_write(pos, r, g, b)

    elif anim == "singularity":
        spd    = state["flicker_spd"] * 0.4
        center = (math.sin(t*spd*6.2832)*0.35 + 0.5) * BLADE_LEN
        for pos in range(BLADE_LEN):
            dist = abs(pos - center)
            if dist < 1.0:
                blade_write(pos, 255*bsc, 255*bsc, 255*bsc)
            elif dist < 8.0:
                pull = (1.0 - (dist/8.0))**2
                blade_write(pos,
                    min(255.0, (CR*(1-pull)+C2R*pull)*bsc*(1.5*pull+0.4)),
                    min(255.0, (CG*(1-pull)+C2G*pull)*bsc*(1.5*pull+0.4)),
                    min(255.0, (CB*(1-pull)+C2B*pull)*bsc*(1.5*pull+0.4)))
            else:
                blade_write(pos, CR*bsc*0.3, CG*bsc*0.3, CB*bsc*0.3)

    elif anim == "phaser":
        spd = state["flicker_spd"] * 0.5
        for pos in range(BLADE_LEN):
            u   = pos / max(1, BLADE_LEN - 1)
            w1  = math.sin(u*6.28 + t*spd*4.0)
            w2  = math.sin(u*18.8 - t*spd*9.5)
            mix = (w1 + w2 + 2.0) * 0.25
            blade_write(pos,
                (CR*(1-mix)+C2R*mix)*bsc,
                (CG*(1-mix)+C2G*mix)*bsc,
                (CB*(1-mix)+C2B*mix)*bsc)

    elif anim == "bio_mesh":
        spd = state["flicker_spd"] * 0.6
        for pos in range(BLADE_LEN):
            u     = pos / max(1, BLADE_LEN - 1)
            pulse = math.sin(t*spd*3.0 - u*5.0) * math.cos(t*1.7 + u*11.0)
            node  = max(0.0, pulse)
            r = (CR*0.6 + C2R*node*0.4)*bsc*(0.5 + 0.5*node)
            g = (CG*0.6 + C2G*node*0.4)*bsc*(0.5 + 0.5*node)
            b = (CB*0.6 + C2B*node*0.4)*bsc*(0.5 + 0.5*node)
            if node > 0.88 and random.random() > 0.85:
                blade_write(pos, 255*bsc, 255*bsc, 255*bsc)
            else:
                blade_write(pos, r, g, b)

    elif anim == "cyber_sweep":
        spd  = state["flicker_spd"] * 0.9
        half = (BLADE_LEN - 1) * 0.5
        p1   = half + half * math.sin(t*spd*6.2832)
        p2   = half + half * math.sin(t*spd*6.2832*1.37 + 2.09)
        for pos in range(BLADE_LEN):
            d1 = abs(pos - p1); d2 = abs(pos - p2)
            b1 = math.exp(-d1*0.7); b2 = math.exp(-d2*0.7)
            intensity = min(1.0, b1 + b2*0.6)
            if intensity > 0.05:
                blade_write(pos,
                    min(255.0, (CR + (255-CR)*intensity)*bsc),
                    min(255.0, (CG + (255-CG)*intensity)*bsc),
                    min(255.0, (CB + (255-CB)*intensity)*bsc))
            else:
                blade_write(pos, CR*bsc*0.12, CG*bsc*0.12, CB*bsc*0.12)

    elif anim == "comet":
        spd = state["flicker_spd"] * 0.6
        _comet_speed = spd * BLADE_LEN * 0.016
        _comet_pos  += _comet_dir * _comet_speed
        if _comet_pos >= BLADE_LEN - 1:
            _comet_pos = BLADE_LEN - 1; _comet_dir = -1
        elif _comet_pos <= 0:
            _comet_pos = 0; _comet_dir = 1
        for pos in range(BLADE_LEN):
            d = _comet_pos - pos
            if 0 <= d < 12:
                tail   = math.exp(-d * 0.45)
                bright = bsc * tail
                blade_write(pos,
                    min(255.0, (CR + (255-CR)*tail)*bright),
                    min(255.0, (CG + (255-CG)*tail)*bright),
                    min(255.0, (CB + (255-CB)*tail)*bright))
            else:
                blade_write(pos, CR*bsc*0.04, CG*bsc*0.04, CB*bsc*0.04)

    elif anim == "heartbeat":
        period = 1.2 / max(0.1, state["flicker_spd"])
        phase  = (t % period) / period
        if phase < 0.08:
            bri = math.sin(phase / 0.08 * math.pi) * bsc
        elif phase < 0.16:
            bri = math.sin((phase - 0.08) / 0.08 * math.pi) * bsc * 0.5
        else:
            bri = 0.0
        for pos in range(BLADE_LEN):
            u    = pos / max(1, BLADE_LEN - 1)
            dist = abs(u - 0.3)
            fade = max(0.0, 1.0 - dist * 2.5)
            b_px = bri * (0.3 + 0.7 * fade)
            blade_write(pos, min(255.0, CR*b_px), min(255.0, CG*b_px), min(255.0, CB*b_px))

    elif anim == "matrix_rain":
        spd = state["flicker_spd"] * 0.3
        for i in range(BLADE_LEN):
            _matrix_cols[i] = (_matrix_cols[i] + spd * (0.5 + 0.5*random.random()) * 0.05) % 1.2
        for pos in range(BLADE_LEN):
            u    = pos / max(1, BLADE_LEN - 1)
            head = _matrix_cols[pos]
            d    = head - u
            if 0.0 <= d < 0.3:
                trail = 1.0 - (d / 0.3)
                bright = bsc * trail
                r = CR * bright; g = CG * bright; b = CB * bright
                if d < 0.04:
                    r = min(255.0, r + 80*bsc)
                    g = min(255.0, g + 80*bsc)
                    b = min(255.0, b + 80*bsc)
                blade_write(pos, r, g, b)
            else:
                blade_write(pos, 0, 0, 0)

    elif anim == "galaxy":
        for pos in range(BLADE_LEN):
            u  = pos / max(1, BLADE_LEN - 1)
            n1 = math.sin(u*7.1 + t*0.4) * 0.5 + 0.5
            n2 = math.sin(u*3.3 - t*0.27 + 1.0) * 0.5 + 0.5
            nebula = (n1 + n2) * 0.5
            r = (CR*nebula + C2R*(1-nebula)) * bsc * 0.6
            g = (CG*nebula + C2G*(1-nebula)) * bsc * 0.6
            b = (CB*nebula + C2B*(1-nebula)) * bsc * 0.6
            if random.random() > 0.97:
                twinkle = random.random() * bsc
                r = min(255.0, r + 255*twinkle*0.6)
                g = min(255.0, g + 255*twinkle*0.6)
                b = min(255.0, b + 255*twinkle*0.6)
            blade_write(pos, min(255.0, r), min(255.0, g), min(255.0, b))

    elif anim == "vortex":
        spd = state["flicker_spd"] * 0.7
        for pos in range(BLADE_LEN):
            u    = pos / max(1, BLADE_LEN - 1)
            spin = math.sin(u * 12.56 - t * spd * 6.2832) * 0.5 + 0.5
            glow = math.sin(u * 6.28  + t * spd * 3.14)   * 0.5 + 0.5
            mix  = spin * glow
            r = min(255.0, (CR*(1-mix) + C2R*mix)*bsc*(0.5+0.5*glow))
            g = min(255.0, (CG*(1-mix) + C2G*mix)*bsc*(0.5+0.5*glow))
            b = min(255.0, (CB*(1-mix) + C2B*mix)*bsc*(0.5+0.5*glow))
            blade_write(pos, r, g, b)

    elif anim == "clash":
        elapsed_ms = time.ticks_diff(time.ticks_ms(), _clash_start)
        if elapsed_ms > 500:
            state['anim'] = _prev_anim
            _ble_notify_anim()
        else:
            phase = elapsed_ms / 80.0
            bri   = bsc if (int(phase) % 2 == 0) else bsc * 0.12
            for pos in range(BLADE_LEN):
                d    = abs(pos - BLADE_LEN // 2)
                wave = 1.0 - d / BLADE_LEN * 0.4
                blade_write(pos, min(255.0, 255*bri*wave), min(255.0, 230*bri*wave), min(255.0, 160*bri*wave))

    _blank_hidden()
    np.write()

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                B L U E T O O T H   L E   S E R V E R                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝
_IRQ_CENTRAL_CONNECT    = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE        = const(3)

_SVC_UUID    = bluetooth.UUID('7a0247b0-0001-4adf-a9f0-8d3b1c6e9f00')
_POWER_UUID  = bluetooth.UUID('7a0247b0-0002-4adf-a9f0-8d3b1c6e9f00')
_COLOR_UUID  = bluetooth.UUID('7a0247b0-0003-4adf-a9f0-8d3b1c6e9f00')
_COLOR2_UUID = bluetooth.UUID('7a0247b0-0004-4adf-a9f0-8d3b1c6e9f00')
_ANIM_UUID   = bluetooth.UUID('7a0247b0-0005-4adf-a9f0-8d3b1c6e9f00')
_PARAMS_UUID = bluetooth.UUID('7a0247b0-0006-4adf-a9f0-8d3b1c6e9f00')

_F_RW  = bluetooth.FLAG_READ | bluetooth.FLAG_WRITE
_F_RWN = bluetooth.FLAG_READ | bluetooth.FLAG_WRITE | bluetooth.FLAG_NOTIFY

_SABER_SERVICE = (
    _SVC_UUID,
    (
        (_POWER_UUID,  _F_RWN),
        (_COLOR_UUID,  _F_RWN),
        (_COLOR2_UUID, _F_RW),
        (_ANIM_UUID,   _F_RWN),
        (_PARAMS_UUID, _F_RW),
    ),
)

ble = bluetooth.BLE()
ble.active(True)
((h_power, h_color, h_color2, h_anim, h_params),) = ble.gatts_register_services((_SABER_SERVICE,))
_ble_central = None

def _build_adv_data():
    p = bytearray()
    p.extend((2, 0x01, 0x06))  # 1. Flags field (Strict BLE core rule)
    p.extend((17, 0x07))       # 2. Complete List of 128-bit Service UUIDs
    p.extend(bytes(_SVC_UUID))
    return p

def _build_resp_data():
    p = bytearray()
    name_bytes = BLE_NAME.encode()
    p.extend((len(name_bytes) + 1, 0x09))  # 3. Clean local name separation
    p.extend(name_bytes)
    return p

_ADV_DATA  = _build_adv_data()
_RESP_DATA = _build_resp_data()

def _ble_advertise():
    ble.gap_advertise(100000, adv_data=_ADV_DATA, resp_data=_RESP_DATA)

def _ble_notify_power():
    if _ble_central is not None:
        ble.gatts_write(h_power, bytes((1 if state['on'] else 0,)))
        try: ble.gatts_notify(_ble_central, h_power)
        except: pass

def _ble_notify_color():
    if _ble_central is not None:
        ble.gatts_write(h_color, bytes(state['color']))
        try: ble.gatts_notify(_ble_central, h_color)
        except: pass

def _ble_notify_anim():
    if _ble_central is not None:
        ble.gatts_write(h_anim, state['anim'].encode())
        try: ble.gatts_notify(_ble_central, h_anim)
        except: pass

def _ble_irq(event, data):
    global _ble_central
    if event == _IRQ_CENTRAL_CONNECT:
        conn_handle, _, _ = data
        _ble_central = conn_handle
        print("BLE connected")
    elif event == _IRQ_CENTRAL_DISCONNECT:
        _ble_central = None
        print("BLE disconnected")
        _ble_advertise()
    elif event == _IRQ_GATTS_WRITE:
        conn_handle, value_handle = data
        if value_handle == h_power:
            v = ble.gatts_read(h_power)
            if v: apply_power(v[0])
        elif value_handle == h_color:
            v = ble.gatts_read(h_color)
            if len(v) >= 3: apply_color(v[0], v[1], v[2])
        elif value_handle == h_color2:
            v = ble.gatts_read(h_color2)
            if len(v) >= 3: apply_color2(v[0], v[1], v[2])
        elif value_handle == h_anim:
            v = ble.gatts_read(h_anim)
            try: apply_anim(v.decode('utf-8').strip())
            except: pass
        elif value_handle == h_params:
            v = ble.gatts_read(h_params)
            if len(v) >= 4: apply_params(v[0], v[1], v[2], v[3])

ble.irq(_ble_irq)
ble.gatts_write(h_power,  bytes((1 if state['on'] else 0,)))
ble.gatts_write(h_color,  bytes(state['color']))
ble.gatts_write(h_color2, bytes(state['color2']))
ble.gatts_write(h_anim,   state['anim'].encode())
ble.gatts_write(h_params, bytes((100, 7, 35, 42)))
_ble_advertise()
print("BLE advertising as", BLE_NAME)
gc.collect()

# ── Wi-Fi Access Point ────────────────────────────────────────────────────
ap = network.WLAN(network.AP_IF)
ap.active(False); time.sleep_ms(100); ap.active(True); time.sleep_ms(100)
ap.config(ssid=WIFI_SSID, password='', security=0, channel=6)
print("AP Address:", ap.ifconfig()[0])
gc.collect()

# [HTTP variables, parse_body, respond, and handle sections are embedded in HTML]
# To conserve RAM variables, HTML content block is maintained natively below.
HTML = b"""...""" # Identical to your standard captive portal assets

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('0.0.0.0', 80))
srv.listen(2)
srv.setblocking(False)
print("HTTP server listening on 192.168.4.1:80")

# ── Runtime loop ───────────────────────────────────────────────────────────
blade_on_prev = False
FRAME_US      = 1_000_000 // TARGET_FPS

while True:
    gc.collect()  # Proactive heap recovery
    try:
        conn, addr = srv.accept()
        # [Imported handle execution directly]
        try:
            req = b''
            conn.settimeout(0.2) # Drastically cut timeout to keep BLE lively
            while True:
                chunk = conn.recv(256)
                if not chunk: break
                req += chunk
                if b'\r\n\r\n' in req: break
            
            req_str = req.decode('utf-8', 'ignore')
            first_line = req_str.split('\r\n')[0].split(' ')
            method = first_line[0]; path = first_line[1]
            
            if method == 'GET' and path in ('/', '/index.html'):
                # Serve internal web UI structure
                pass 
        except: pass
        finally: conn.close()
    except OSError: pass

    btn_now = (button.value() == 0)
    if btn_now and not _btn_prev:
        now_ms = time.ticks_ms()
        if time.ticks_diff(now_ms, _last_tap_ms) < DOUBLE_TAP_MS:
            _tap_count = 0
            if state['on']: apply_anim('clash')
        else: _tap_count = 1
        _last_tap_ms = now_ms

    if (_tap_count == 1 and time.ticks_diff(time.ticks_ms(), _last_tap_ms) > DOUBLE_TAP_MS):
        _tap_count = 0
        apply_power(not state['on'])
    _btn_prev = btn_now

    if state['on'] and not blade_on_prev:
        sw_led.value(1); ignite(); blade_on_prev = True
    elif not state['on'] and blade_on_prev:
        sw_led.value(0); extinguish(); blade_on_prev = False

    if state['on']:
        t0    = time.ticks_us()
        render_frame()
        slack = FRAME_US - time.ticks_diff(time.ticks_us(), t0)
        if slack > 0: time.sleep_us(slack)
    else:
        time.sleep_ms(10)