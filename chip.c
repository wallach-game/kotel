#include "velxio-chip.h"
#include <stdlib.h>
#include <stdio.h>

/* Fixed "how hot/cold the sensor reads near the burner" swing around
 * setpoint, in C. Not user-tunable (the spec names only sensor_tau_s and
 * hysteresis_k) — it's the internal amplitude the RC-relaxation math below
 * is derived against so the DEFAULT sensor_tau_s/hysteresis_k land the
 * emergent cycle period near cycle_period_s. See docs/boiler-timing.md. */
#define SENSOR_SWING_C 2.0

typedef enum {
    STATE_OFF = 0,
    STATE_STARTING = 1,
    STATE_HEATING = 2,
} boiler_state_t;

typedef struct {
    vx_pin on_off;
    vx_pin ref_12v;
    vx_pin temp_in;

    vx_timer timer;

    /* Configurable parameters (vx_attr_register — live-tunable, no recompile). */
    vx_attr ignition_delay_s;
    vx_attr cycle_period_s;   /* documentation/reference only — see SENSOR_SWING_C comment */
    vx_attr sensor_tau_s;
    vx_attr hysteresis_k;

    double water_temp;
    double target_temp;      /* setpoint, from TEMP_IN */
    double sensor_temp;      /* fast-lag internal sensor (drives hysteresis) */

    boiler_state_t state;
    int prev_demand;
    int starting_elapsed_s;
    int burner_on;
    int heating;              /* legacy alias of burner_on, kept for the display bar color */
    int tick_count;

    vx_buffer fb;
    uint32_t  fb_w;
    uint32_t  fb_h;
} chip_state_t;


/* 3x5 pixel font, bit 2 = left column .. bit 0 = right column, top row first.
 * Only the glyphs the stats line needs: digits, ':', 'E', 'S', 'T'. */
static const uint8_t *font_glyph(char c)
{
    static const uint8_t GLYPHS[][5] = {
        {0b111,0b101,0b101,0b101,0b111}, /* 0 */
        {0b010,0b110,0b010,0b010,0b111}, /* 1 */
        {0b111,0b001,0b111,0b100,0b111}, /* 2 */
        {0b111,0b001,0b111,0b001,0b111}, /* 3 */
        {0b101,0b101,0b111,0b001,0b001}, /* 4 */
        {0b111,0b100,0b111,0b001,0b111}, /* 5 */
        {0b111,0b100,0b111,0b101,0b111}, /* 6 */
        {0b111,0b001,0b001,0b001,0b001}, /* 7 */
        {0b111,0b101,0b111,0b101,0b111}, /* 8 */
        {0b111,0b101,0b111,0b001,0b111}, /* 9 */
        {0b000,0b010,0b000,0b010,0b000}, /* : */
        {0b111,0b100,0b110,0b100,0b111}, /* E */
        {0b111,0b100,0b111,0b001,0b111}, /* S */
        {0b111,0b010,0b010,0b010,0b010}, /* T */
        {0b111,0b100,0b110,0b100,0b100}, /* M (reuses E's stem — legible enough at 3px) */
        {0b000,0b000,0b000,0b000,0b000}, /* unknown / space */
    };
    switch (c) {
        case '0': case '1': case '2': case '3': case '4':
        case '5': case '6': case '7': case '8': case '9':
            return GLYPHS[c - '0'];
        case ':': return GLYPHS[10];
        case 'E': return GLYPHS[11];
        case 'S': return GLYPHS[12];
        case 'T': return GLYPHS[13];
        case 'M': return GLYPHS[14];
        default:  return GLYPHS[15];
    }
}

/* Draws each glyph on an opaque background cell so the stats line stays
 * legible over whatever's already in the framebuffer (the bar/fill color). */
static void draw_text(chip_state_t *s, uint32_t x0, uint32_t y0, const char *str,
                       const uint8_t fg[4], const uint8_t bg[4])
{
    uint32_t x = x0;
    for (const char *p = str; *p; p++) {
        for (uint32_t dy = 0; dy < 6; dy++)
            for (uint32_t dx = 0; dx < 4; dx++)
                vx_buffer_write(s->fb, ((y0 + dy) * s->fb_w + (x + dx)) * 4, bg, 4);

        const uint8_t *rows = font_glyph(*p);
        for (uint32_t row = 0; row < 5; row++) {
            uint8_t bits = rows[row];
            for (uint32_t col = 0; col < 3; col++) {
                if (bits & (1 << (2 - col)))
                    vx_buffer_write(s->fb, ((y0 + row) * s->fb_w + (x + col)) * 4, fg, 4);
            }
        }
        x += 4;
    }
}

static void draw_display(chip_state_t *s)
{
    if (s->fb < 0) return;   /* no display declared in chip.json / headless host */

    uint8_t bg[4]     = {0x18, 0x18, 0x20, 0xFF};
    uint8_t idle[4]   = {0x30, 0x70, 0xE0, 0xFF};
    uint8_t heat[4]   = {0xE0, 0x40, 0x30, 0xFF};
    uint8_t target[4] = {0xFF, 0xFF, 0xFF, 0xFF};
    uint8_t *fill = s->heating ? heat : idle;

    /* Bar spans 0-100C over the full canvas height; a white line marks target_temp. */
    double frac = s->water_temp / 100.0;
    if (frac < 0.0) frac = 0.0;
    if (frac > 1.0) frac = 1.0;
    uint32_t fill_rows = (uint32_t)(frac * s->fb_h);

    double target_frac = s->target_temp / 100.0;
    if (target_frac < 0.0) target_frac = 0.0;
    if (target_frac > 1.0) target_frac = 1.0;
    uint32_t target_row = s->fb_h - 1 - (uint32_t)(target_frac * s->fb_h);

    for (uint32_t y = 0; y < s->fb_h; y++) {
        int is_fill = (y >= s->fb_h - fill_rows);
        uint8_t *row_color = (y == target_row) ? target : (is_fill ? fill : bg);
        for (uint32_t x = 0; x < s->fb_w; x++) {
            vx_buffer_write(s->fb, (y * s->fb_w + x) * 4, row_color, 4);
        }
    }

    char stats[24];
    snprintf(stats, sizeof stats, "M:%d S:%.0f T:%.0f", (int)s->state, s->target_temp, s->water_temp);
    uint8_t text_fg[4] = {0xFF, 0xFF, 0xFF, 0xFF};
    uint8_t text_bg[4] = {0x00, 0x00, 0x00, 0xFF};
    draw_text(s, 1, 1, stats, text_fg, text_bg);
}

static void boiler_tick(void *ud)
{
    chip_state_t *s = (chip_state_t *)ud;
    s->tick_count++;

    /* TEMP pin sets the setpoint: 0V -> 35C, 12V -> 80C. */
    double temp_voltage_raw = vx_pin_read_analog(s->temp_in);
    double temp_voltage = temp_voltage_raw;
    if (temp_voltage < 0.0) temp_voltage = 0.0;
    if (temp_voltage > 12.0) temp_voltage = 12.0;
    s->target_temp = 35.0 + temp_voltage * (45.0 / 12.0);

    int demand = vx_pin_read(s->on_off) != 0;

    /* ── 1. Ignition-delay state machine ─────────────────────────────── */
    switch (s->state) {
        case STATE_OFF:
            if (demand && !s->prev_demand) {
                s->state = STATE_STARTING;
                s->starting_elapsed_s = 0;
            }
            break;

        case STATE_STARTING:
            if (!demand) {
                s->state = STATE_OFF;   /* demand dropped mid-delay: abort, no heat produced */
            } else {
                s->starting_elapsed_s++;
                if (s->starting_elapsed_s >= (int)vx_attr_read(s->ignition_delay_s)) {
                    s->state = STATE_HEATING;
                    s->burner_on = 1;
                    s->sensor_temp = s->water_temp;   /* sensor picks up from whatever the water is now */
                }
            }
            break;

        case STATE_HEATING:
            if (!demand) {
                s->state = STATE_OFF;
                s->burner_on = 0;
            }
            break;
    }
    s->prev_demand = demand;
    if (s->state != STATE_HEATING) s->burner_on = 0;

    /* ── 2. Cycling around setpoint — emergent, not a scheduled timer ───
     * The internal sensor has low thermal mass: it swings toward a
     * burner-driven local reference (+-SENSOR_SWING_C around setpoint)
     * much faster than the bulk water does. That lag is what drives the
     * hysteresis controller into a limit cycle — an RC-relaxation
     * oscillator, same topology as a 555 astable. Only runs while
     * actually heating; off/starting produce no heat, so there's nothing
     * to cycle yet. */
    if (s->state == STATE_HEATING) {
        double tau = vx_attr_read(s->sensor_tau_s);
        if (tau < 0.1) tau = 0.1;
        double ref = s->burner_on ? (s->target_temp + SENSOR_SWING_C)
                                   : (s->target_temp - SENSOR_SWING_C);
        s->sensor_temp += (ref - s->sensor_temp) * (1.0 / tau);

        double half = vx_attr_read(s->hysteresis_k) / 2.0;
        if (s->sensor_temp <= s->target_temp - half) s->burner_on = 1;
        else if (s->sensor_temp >= s->target_temp + half) s->burner_on = 0;
    }

    /* ── 3. Water thermal model (slow bulk mass) ─────────────────────── */
    if (s->burner_on) {
        s->water_temp += 0.1;
    } else if (s->water_temp > 20.0) {
        s->water_temp -= 0.02;
    }
    s->heating = s->burner_on;

    printf(
        "Kotel: t=%d demand=%d burner_on=%d sensor_temp=%.2f water_temp=%.2f state=%d target=%.1fC\n",
        s->tick_count, demand, s->burner_on, s->sensor_temp, s->water_temp, (int)s->state, s->target_temp
    );
    fflush(stdout);

    draw_display(s);
}


void chip_setup(void)
{
    chip_state_t *s = calloc(1, sizeof(*s));

    if (!s) { vx_log("Kotel: allocation failed"); return; }

    s->on_off  = vx_pin_register("ON/OFF", VX_INPUT);
    s->ref_12v = vx_pin_register("12V REF", VX_ANALOG);
    vx_pin_dac_write(s->ref_12v, 5.0);
    s->temp_in = vx_pin_register("TEMP 0-12V", VX_ANALOG);

    s->ignition_delay_s = vx_attr_register("ignition_delay_s", 30.0);
    s->cycle_period_s   = vx_attr_register("cycle_period_s", 120.0);
    s->sensor_tau_s      = vx_attr_register("sensor_tau_s", 30.0);
    s->hysteresis_k      = vx_attr_register("hysteresis_k", 3.0);

    s->water_temp = 20.0;
    s->sensor_temp = 20.0;
    s->state = STATE_OFF;

    s->fb = vx_framebuffer_init(&s->fb_w, &s->fb_h);
    draw_display(s);

    /* Run boiler_tick() every simulated second. */
    s->timer = vx_timer_create(boiler_tick, s);
    vx_timer_start(s->timer, 1000000000ULL, true);

    vx_log("Kotel: initialized");
}
