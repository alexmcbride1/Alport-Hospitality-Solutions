# Alport website maintenance

The user requires the logged-in website guide to stay in step with website additions and changes.

For every user-facing feature, navigation, role/access or workflow change:
- Review its impact on `/user-guide` and `private_docs/Alport-Website-User-Guide.pdf`.
- Update the guide text, navigation diagrams and affected screenshots to match the deployed UI. Do not document unshipped screens as live.
- Use `docs/user-guide/build_guide.py` and the checked-in screenshot assets to rebuild the PDF. Follow `docs/user-guide/README.md` for capture and validation.
- Keep the guide landing page's page count, descriptions and revision in sync.
- Confirm the guide and PDF download still require login. Preserve the current authentication and business-data permissions.
- Report the guide impact, captured deployment revision and validation with the website change. If deployment is pending, explicitly hand off the guide refresh to the Alport website manager instead of claiming the work is complete.

Preserve other workers' changes by checking the latest branch before publishing. Website manager coordination is handled by the user's existing Alport website manager chat; do not create duplicate recurring guide checks.
