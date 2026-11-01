/* Download the last successfully displayed month; never submit browser totals. */
async function exportFinancePDF() {
    const button = document.getElementById('financeExport');
    const status = document.getElementById('financeExportStatus');
    if (!financeReportMonth || button.disabled) return;
    const selectedMonth = financeReportMonth;
    const siteId = button.dataset.siteId;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    status.textContent = 'Preparing your Alport PDF…';
    try {
        const query = new URLSearchParams({month: selectedMonth, site_id: siteId});
        const response = await fetch('/api/finance/export.pdf?' + query, {cache: 'no-store'});
        if (!response.ok || response.redirected || !(response.headers.get('Content-Type') || '').includes('application/pdf')) {
            const detail = await response.json().catch(() => ({}));
            throw new Error(detail.error || 'Unable to export. Check your sign-in and try again.');
        }
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `alport-finance-site-${siteId}-${selectedMonth}.pdf`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
        status.textContent = `PDF download started for ${selectedMonth}.`;
    } catch (error) {
        status.textContent = error.message;
    } finally {
        button.disabled = !financeReportMonth;
        button.removeAttribute('aria-busy');
    }
}
