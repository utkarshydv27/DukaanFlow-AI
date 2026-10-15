(function () {
  const toast = document.getElementById('toast');
  let toastTimer;
  function showToast(message, type = 'success') {
    if (!toast) return;
    toast.textContent = message;
    toast.className = `toast show ${type}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.className = 'toast', 4200);
  }

  document.querySelectorAll('[data-action]').forEach((button) => {
    button.addEventListener('click', async () => {
      const id = button.dataset.id;
      const action = button.dataset.action;
      if (action === 'confirm' && !window.confirm("Confirm skipping this customer's tiffin for today only? Their regular subscription will stay active.")) return;
      button.disabled = true;
      try {
        const response = await fetch(`/api/requests/${id}/${action}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}'
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'Action failed.');
        showToast(data.message || (action === 'confirm' ? 'Skip confirmed.' : 'Request moved to review.'));
        setTimeout(() => window.location.reload(), 650);
      } catch (error) {
        showToast(error.message || 'Something went wrong.', 'error');
        button.disabled = false;
      }
    });
  });

  const customerToggle = document.getElementById('toggle-customer-form');
  const customerForm = document.getElementById('customer-form');
  if (customerToggle && customerForm) {
    customerToggle.addEventListener('click', () => {
      customerForm.hidden = !customerForm.hidden;
      if (!customerForm.hidden) document.getElementById('customer-name')?.focus();
    });
    customerForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = customerForm.querySelector('button[type="submit"]');
      button.disabled = true;
      try {
        const payload = Object.fromEntries(new FormData(customerForm).entries());
        const response = await fetch('/api/customers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'Unable to save customer.');
        showToast(data.message || 'Customer saved.');
        setTimeout(() => window.location.reload(), 650);
      } catch (error) {
        showToast(error.message || 'Unable to save customer.', 'error');
      } finally { button.disabled = false; }
    });
  }

  const demoForm = document.getElementById('demo-form');
  if (demoForm) {
    demoForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = demoForm.querySelector('button[type="submit"]');
      button.disabled = true;
      button.classList.add('is-loading');
      try {
        const payload = Object.fromEntries(new FormData(demoForm).entries());
        const response = await fetch('/api/demo/message', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(data.error || 'Unable to analyze message.');
        if (data.duplicate) showToast(data.reason || 'Duplicate message ignored.', 'error');
        else showToast(`Message analyzed · ${data.status.replace('_', ' ')}${data.customer_name ? ' · ' + data.customer_name : ''}`);
        setTimeout(() => window.location.reload(), 650);
      } catch (error) {
        showToast(error.message || 'Something went wrong.', 'error');
      } finally {
        button.disabled = false;
        button.classList.remove('is-loading');
      }
    });
  }
})();
