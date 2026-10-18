# Issue Taxonomy for Dogfood Testing

## Severity Levels

| Level | Label | Description | Example |
|---|---|---|---|
| S1 | Critical | App crash, data loss, security vulnerability | Page won't load, JS unhandled exception |
| S2 | Major | Feature broken, workflow blocked | Form submit fails, navigation loop |
| S3 | Minor | Cosmetic issue, workaround exists | Misaligned button, truncated text |
| S4 | Enhancement | Not a bug, but could be improved | Slow load, confusing label |

## Categories

### Visual (V)
- Layout breaks, overlapping elements
- Missing/broken images
- Responsive design failures
- CSS rendering issues

### Functional (F)
- Buttons/links not working
- Forms not submitting
- Data not loading/saving
- Incorrect calculations or logic

### UX (U)
- Confusing navigation flow
- Missing feedback (loading states, confirmations)  
- Accessibility issues (contrast, screen reader)
- Inconsistent behavior

### Content (C)
- Typos, grammatical errors
- Outdated/incorrect information
- Missing translations

### Performance (P)
- Slow page loads (>3s)
- Unresponsive interactions
- Memory leaks (increasing console errors)
- Large asset sizes

### Console (N)
- JavaScript errors in console
- Failed network requests (4xx/5xx)
- Deprecation warnings
- CORS errors

### Accessibility (A)
- Missing alt text
- Keyboard navigation issues
- Screen reader incompatibility
- Insufficient color contrast

## Exploration Checklist

- [ ] Main navigation — all links work?
- [ ] Forms — submit, validation, error states?
- [ ] Authentication — login, logout, session persistence?
- [ ] Data display — lists, tables, pagination?
- [ ] Responsive — mobile, tablet, desktop?
- [ ] Edge cases — empty states, long content, special chars?
- [ ] Error handling — 404, network failure, invalid input?
- [ ] Console errors — any JS errors or warnings?
- [ ] Performance — page load times acceptable?
- [ ] Accessibility — keyboard nav, contrast, labels?
