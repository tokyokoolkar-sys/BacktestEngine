---
name: gemini-image
description: Automate image generation on Google Gemini (gemini.google.com) using CamoFox CLI. Use when the user asks to "generate image", "create image with Gemini", "AI image generation", "tạo hình ảnh", or needs automated Gemini workflows.
allowed-tools: Bash(camofox:*)
---

# Gemini Image Generation Skill (CLI)

Use this skill for Gemini image generation flows with CamoFox CLI commands only. Use `curl` only for download file retrieval from the local CamoFox download API.

## 1. Prerequisites

- CamoFox server must be running on port `9377`.
- Google account with Gemini access.
- First-time login requires email/password and 2FA approval on phone.
- After first login, save and reuse a session profile.

## 2. Quick Start (Fast Path — Authenticated)

When a valid Gemini session is already saved:

All commands below assume `--user gemini`. Add it explicitly in multi-session scenarios.

```bash
# Load saved session and open Gemini
camofox open https://gemini.google.com/app --user gemini

# Load saved profile
camofox session load <profile-name> --user gemini

# Verify auth (check for profile avatar, no "Sign in" button)
camofox snapshot --user gemini

# Type image prompt
camofox type <prompt-ref> "your image description here" --user gemini

# Submit prompt
camofox press Enter --user gemini

# Wait for image generation to complete (15-180 seconds)
camofox wait 'mat-icon[fonticon="download"]' --timeout 180000 --user gemini

# Download the generated image
camofox click 'mat-icon[fonticon="download"]' --user gemini

# Cleanup
camofox close --user gemini
```

## 3. Authentication Flow

### Step 3.1: Check Auth State

```bash
camofox open https://gemini.google.com/app --user gemini
camofox snapshot --user gemini
# Look for: "Sign in" button -> NOT authenticated
# Look for: Greeting text "Xin chào..." -> authenticated
```

### Step 3.2: Login (if needed)

```bash
# Navigate to Google login
camofox navigate https://accounts.google.com --user gemini

# Enter email
camofox fill '[e1]="email@gmail.com"' --user gemini
# OR
camofox type e1 "email@gmail.com" --user gemini
camofox click e4 --user gemini  # Next button

# Wait for password page
camofox wait 'input[type="password"]' --user gemini
camofox snapshot --user gemini

# Enter password
camofox type e2 "password" --user gemini
camofox click e4 --user gemini  # Next button
```

> **Security Tip:** For production workflows, store credentials in CamoFox Auth Vault:
> ```bash
> camofox auth save google-gemini
> # Follow interactive prompts to save credentials securely
> camofox auth load google-gemini --user gemini
> ```

### Step 3.3: 2FA (Push Notification)

After entering password, Google may prompt 2FA:

- Push notification: user taps "Yes" on phone app.
- Wait for redirect:

```bash
camofox wait 'a[href*="myaccount.google.com"]' --timeout 90000 --user gemini
```

- Or wait for URL change away from `accounts.google.com`:

```bash
camofox wait navigation --timeout 90000 --user gemini
```

### Step 3.4: Save Session

```bash
camofox session save <profile-name> --user gemini
# Reuse next time with: camofox session load <profile-name> --user gemini
```

## 4. Image Generation

```bash
# Navigate to Gemini (if not already there)
camofox navigate https://gemini.google.com/app --user gemini

# Snapshot to find prompt input ref
camofox snapshot --user gemini
# Prompt textbox is typically the main textarea element

# Type your image description
camofox type <prompt-ref> "Generate an image of a cute red fox wearing sunglasses in digital art style" --user gemini

# Submit the prompt
camofox press Enter --user gemini

# Wait for generation to complete (15-180 seconds)
# The download button appears when generation is done
camofox wait 'mat-icon[fonticon="download"]' --timeout 180000 --user gemini

# Verify the result
camofox snapshot --user gemini
# Key refs after generation:
# - Download button (mat-icon fonticon="download")
# - Like (mat-icon fonticon="thumb_up")
# - Dislike (mat-icon fonticon="thumb_down")
# - Redo
# - Share
```

## 5. Download Generated Image

```bash
# Click download button (locale-independent CSS selector)
camofox click 'mat-icon[fonticon="download"]' --user gemini

# Check download status
camofox downloads --format json --user gemini
# Parse `downloadId` from JSON output (for example with `jq -r '.[0].id'`)

# Retrieve the file via REST API
# The click response or downloads command shows the download ID
curl -o "generated-image.png" "http://localhost:9377/downloads/<download-id>/content?userId=gemini"

# Cleanup
camofox close --user gemini
```

## 6. Tips & Best Practices

- **Session reuse**: Always save profile after login. Load before each run to skip login.
- **Locale-agnostic selectors**: Use `mat-icon[fonticon="..."]` instead of text labels.
- **Wait instead of sleep**: Use `camofox wait` with CSS selectors, not fixed delays.
- **Re-snapshot after actions**: Element refs (`eN`) shift after navigation/interaction.
- **Image prompts**: Be descriptive; Gemini performs better with detailed prompts.
- **Rate limits**: Free tier has daily image generation limits. If limit is reached, wait or switch account.

## 7. Error Recovery

| Error | Solution |
|---|---|
| "Sign in" still visible after session load | Session expired (24-48h). Re-login with email/password + 2FA |
| `wait` times out for download button | Image generation may have failed. Run `camofox snapshot` and inspect error text |
| "can't create it right now" | Not authenticated. Re-check auth state and login again |
| "Image Generation Limit Reached" | Daily limit reached. Wait 24h or use another account |
| `TAB_NOT_FOUND` | Tab was invalidated. Open a new tab with `camofox open` |
| `type` does not input text | Gemini may use custom editor. Fallback: `camofox eval 'document.querySelector(".ql-editor").innerHTML = "prompt"'` |

## 8. Verified Test Results

- Account: `user@gmail.com` (User)
- Profile: `gemini-sky` (52 cookies)
- Test prompt: `Generate an image of a cute red fox wearing sunglasses in digital art style`
- Result: `8.1MB PNG` (`Gemini_Generated_Image_ecu3ujecu3ujecu3.png`)
- Generation time: `~30 seconds`
- Download method: `click mat-icon[fonticon="download"]`
