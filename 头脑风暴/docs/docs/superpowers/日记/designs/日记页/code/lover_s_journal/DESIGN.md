---
name: Lover's Journal
colors:
  surface: '#fff8f7'
  surface-dim: '#e7d6d7'
  surface-bright: '#fff8f7'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#fff0f1'
  surface-container: '#fbeaeb'
  surface-container-high: '#f6e4e5'
  surface-container-highest: '#f0dee0'
  on-surface: '#22191a'
  on-surface-variant: '#544244'
  inverse-surface: '#382e2f'
  inverse-on-surface: '#feedee'
  outline: '#877274'
  outline-variant: '#dac0c3'
  surface-tint: '#9b4053'
  primary: '#9b4053'
  on-primary: '#ffffff'
  primary-container: '#ff8fa3'
  on-primary-container: '#782539'
  inverse-primary: '#ffb2bd'
  secondary: '#24657e'
  on-secondary: '#ffffff'
  secondary-container: '#a6e2ff'
  on-secondary-container: '#24667f'
  tertiary: '#7d5800'
  on-tertiary: '#ffffff'
  tertiary-container: '#e6a500'
  on-tertiary-container: '#5a3e00'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#ffd9dd'
  primary-fixed-dim: '#ffb2bd'
  on-primary-fixed: '#400014'
  on-primary-fixed-variant: '#7d283c'
  secondary-fixed: '#bde9ff'
  secondary-fixed-dim: '#93cfeb'
  on-secondary-fixed: '#001f2a'
  on-secondary-fixed-variant: '#004d64'
  tertiary-fixed: '#ffdea9'
  tertiary-fixed-dim: '#ffba27'
  on-tertiary-fixed: '#271900'
  on-tertiary-fixed-variant: '#5e4100'
  background: '#fff8f7'
  on-background: '#22191a'
  surface-variant: '#f0dee0'
typography:
  display-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 48px
    fontWeight: '700'
    lineHeight: 56px
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 32px
    fontWeight: '700'
    lineHeight: 40px
  headline-lg-mobile:
    fontFamily: Plus Jakarta Sans
    fontSize: 28px
    fontWeight: '700'
    lineHeight: 36px
  title-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 20px
    fontWeight: '600'
    lineHeight: 28px
  body-lg:
    fontFamily: Be Vietnam Pro
    fontSize: 18px
    fontWeight: '400'
    lineHeight: 28px
  body-md:
    fontFamily: Be Vietnam Pro
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  label-md:
    fontFamily: Be Vietnam Pro
    fontSize: 14px
    fontWeight: '600'
    lineHeight: 20px
    letterSpacing: 0.01em
  caption:
    fontFamily: Be Vietnam Pro
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 16px
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  base: 8px
  xs: 4px
  sm: 12px
  md: 20px
  lg: 32px
  xl: 48px
  container-padding: 24px
  gutter: 16px
---

## Brand & Style

The brand personality of the design system is sweet, intimate, and encouraging. It aims to create a safe, digital sanctuary for couples to document their shared journey. The emotional response should be one of "cozy nostalgia"—like looking through a physical scrapbook filled with polaroids and hand-written notes.

The design style is **Warm Tactile**. It blends high-quality digital interfaces with organic, hand-drawn sensibilities. This is achieved through:
- **Soft Geometry:** Avoiding harsh angles in favor of generous, organic curves.
- **Illustration-Forward:** Heavy use of doodle-style icons and squiggly dividers to break the "grid" feel.
- **Subtle Depth:** Using soft, colored shadows that mimic paper layers rather than digital elevations.
- **Warm Contrast:** Avoiding pure blacks and whites to maintain a gentle, low-strain visual environment.

## Colors

The palette is built on a "Harmony of Opposites," representing the coming together of two individuals.

- **Primary (Rose Pink - #FF8FA3):** Used for "Her" actions, heart icons, and celebratory moments.
- **Secondary (Sky Blue - #8ECAE6):** Used for "Him" actions, calm notifications, and secondary navigation.
- **Tertiary (Sunset Orange - #FFB703):** An energetic accent reserved for gamified elements, "bets," and highlighting streaks.
- **Neutral (Warm Cream - #FFF9F2):** The base surface for all views, providing a softer alternative to white.
- **Typography (Deep Cocoa - #4A3728):** Used for all text to ensure high legibility while maintaining the warm, organic feel of the brand.

## Typography

This design system uses **Plus Jakarta Sans** for headings to provide a soft, rounded, and modern feel that mimics high-end editorial stationery. For body text, **Be Vietnam Pro** is utilized for its exceptional readability and friendly, contemporary letterforms.

To achieve the "hand-drawn" request without sacrificing accessibility, key headings should occasionally use *italic* variants or be paired with small doodle underlines. Keep line lengths short for body text (max 60 characters) to enhance the diary-like feel.

## Layout & Spacing

The layout philosophy follows a **Fluid "Safe-Zone" Grid**. Rather than rigid columns, content is housed in large, padded containers that feel like loose sheets of paper.

- **Margins:** A generous 24px horizontal margin on mobile ensures the UI feels "airy."
- **Rhythm:** An 8px-based spacing system is used, but preferred increments are larger (20px, 32px) to prevent the UI from feeling cluttered.
- **Reflow:** On tablet/desktop, cards should not stretch to full width; instead, they should cluster into a multi-column "masonry" layout, mimicking a physical scrapbook layout.

## Elevation & Depth

Depth is conveyed through **Tonal Stacking** and **Soft Ambient Shadows**.

1.  **Level 0 (Base):** Warm Cream (#FFF9F2).
2.  **Level 1 (Cards):** Pure White (#FFFFFF) with a soft shadow. Shadows should not be grey; use a tint of the text color (e.g., Deep Cocoa at 8% opacity) with a large blur (16px+) and a slight Y-offset (4px).
3.  **Level 2 (Floating Action Buttons):** These use the Primary or Secondary colors and have a more pronounced shadow (12% opacity) to suggest they are "hovering" over the page.

**Glassmorphism** is used sparingly for top navigation bars (80% opacity blur) to allow the colors of the diary content to peek through as the user scrolls.

## Shapes

The shape language is defined by **Extreme Rounding**.

- **Cards & Primary Containers:** Use a minimum radius of 24px (`rounded-xl`).
- **Buttons & Chips:** Use fully rounded "pill" shapes.
- **Images:** Should always have a 16px radius.
- **Doodle Elements:** Occasional decorative elements (like highlight rings or "wobbly" borders) should be used to frame important memories or dates.

## Components

### Buttons
- **Primary:** Pill-shaped, Rose Pink background, white text. Large internal padding (16px 32px).
- **Secondary:** Pill-shaped, Sky Blue background, white text.
- **Floating Action Button (FAB):** Always circular, containing a simple doodle-style icon. Positioned at the bottom-right with a "bounce" interaction on press.

### Cards
- White background, 24px corner radius.
- Padding should be generous (24px).
- Include a subtle 1px border in a slightly darker cream (#F2E8DF) to define edges against the background.

### Input Fields
- Soft-filled backgrounds (#F2E8DF).
- No borders, only a change in background color on focus (to white).
- Placeholder text in a muted Cocoa.

### Timeline Indicators
- Vertical dotted lines using the Cocoa color.
- "Milestone" indicators should be Rose Pink Hearts.
- "Daily" indicators should be simple Cocoa dots.

### Chips
- Used for mood tagging.
- Small pill shapes with 12px font size.
- Backgrounds should use 15% opacity versions of the Primary/Secondary colors.
