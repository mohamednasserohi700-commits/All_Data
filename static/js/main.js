// ── Toast Notifications ──────────────────────────────────────────────────────
function showToast(message, type = 'success') {
  const container = document.getElementById('toast-container') || createToastContainer();
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  const icon = type === 'success' ? '✅' : '❌';
  toast.innerHTML = `<span>${icon}</span><span>${message}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.animation = 'toast-out 0.4s ease forwards';
    setTimeout(() => toast.remove(), 400);
  }, 3500);
}
function createToastContainer() {
  const c = document.createElement('div');
  c.id = 'toast-container';
  c.className = 'toast-container';
  document.body.appendChild(c);
  return c;
}

// ── Modal Management ─────────────────────────────────────────────────────────
function openModal(id) {
  const modal = document.getElementById(id);
  if (modal) { modal.classList.add('active'); document.body.style.overflow = 'hidden'; }
}
function closeModal(id) {
  const modal = document.getElementById(id);
  if (modal) { modal.classList.remove('active'); document.body.style.overflow = ''; }
}
function closeAllModals() {
  document.querySelectorAll('.modal-overlay').forEach(m => m.classList.remove('active'));
  document.body.style.overflow = '';
}
// Close modal on overlay click
document.addEventListener('click', e => {
  if (e.target.classList.contains('modal-overlay')) closeAllModals();
});

// ── AJAX Helper ──────────────────────────────────────────────────────────────
async function postForm(url, formData) {
  try {
    const res = await fetch(url, { method: 'POST', body: formData });
    if (!res.ok) return { success: false, message: `خطأ في الاتصال (${res.status})` };
    return await res.json();
  } catch (e) {
    console.error('postForm error:', e);
    return { success: false, message: 'تعذّر الاتصال بالخادم، حاول مجدداً' };
  }
}
async function getJSON(url) {
  try {
    const res = await fetch(url);
    if (!res.ok) return { error: `خطأ في الاتصال (${res.status})` };
    return await res.json();
  } catch (e) {
    console.error('getJSON error:', e);
    return { error: 'تعذّر الاتصال بالخادم' };
  }
}

// ── Live Clock ───────────────────────────────────────────────────────────────
function updateClock() {
  const el = document.getElementById('live-clock');
  if (!el) return;
  const now = new Date();
  const opts = { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false };
  el.textContent = now.toLocaleTimeString('ar-SA', opts);
}
setInterval(updateClock, 1000);
updateClock();

// ── Search Debounce ───────────────────────────────────────────────────────────
function debounce(fn, delay = 300) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), delay); };
}

// ── Auto Submit Search ────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const searchInputs = document.querySelectorAll('.auto-search');
  searchInputs.forEach(input => {
    input.addEventListener('input', debounce(() => {
      input.closest('form')?.submit();
    }, 400));
  });

  // Animate stat numbers
  document.querySelectorAll('.stat-value[data-value]').forEach(el => {
    const target = parseInt(el.dataset.value);
    let current = 0;
    const step = Math.ceil(target / 30);
    const timer = setInterval(() => {
      current = Math.min(current + step, target);
      el.textContent = current.toLocaleString('ar-SA');
      if (current >= target) clearInterval(timer);
    }, 40);
  });
});

// ── Users Module ─────────────────────────────────────────────────────────────
function openAddUserModal() {
  document.getElementById('user-form')?.reset();
  document.getElementById('user-id').value = '';
  document.getElementById('user-modal-title').textContent = 'إضافة مستخدم جديد';
  document.getElementById('password-hint').style.display = 'none';
  openModal('user-modal');
}

async function openEditUserModal(id) {
  const data = await getJSON(`/users/edit/${id}`);
  if (data.error) { showToast(data.error, 'error'); return; }
  document.getElementById('user-id').value = data.id;
  document.getElementById('user-gpit').value = data.gpit || '';
  document.getElementById('user-username').value = data.username || '';
  document.getElementById('user-password').value = '';
  document.getElementById('user-location').value = data.location || '';
  document.getElementById('user-national-id').value = data.national_id || '';
  document.getElementById('user-security-q1').value = data.security_q1 || '';
  const q2El = document.getElementById('user-security-q2');
  if (q2El) q2El.value = data.security_q2 || '';
  document.getElementById('user-hire-date').value = data.hire_date || '';
  document.getElementById('user-modal-title').textContent = 'تعديل بيانات المستخدم';
  document.getElementById('password-hint').style.display = 'block';
  openModal('user-modal');
}

async function saveUser() {
  const id = document.getElementById('user-id').value;
  const form = document.getElementById('user-form');
  const fd = new FormData(form);
  const url = id ? `/users/edit/${id}` : '/users/add';
  const data = await postForm(url, fd);
  if (data.success) { showToast(data.message); setTimeout(() => location.reload(), 800); }
  else showToast(data.message || 'حدث خطأ', 'error');
}

async function deleteUser(id, name) {
  if (!confirm(`هل تريد حذف المستخدم "${name}"؟`)) return;
  const fd = new FormData();
  const data = await postForm(`/users/delete/${id}`, fd);
  if (data.success) { showToast(data.message); setTimeout(() => location.reload(), 600); }
  else showToast('حدث خطأ', 'error');
}

// ── SAP Module ────────────────────────────────────────────────────────────────
function openAddSapModal() {
  document.getElementById('sap-form')?.reset();
  document.getElementById('sap-id').value = '';
  document.getElementById('sap-modal-title').textContent = 'إضافة معاملة SAP جديدة';
  openModal('sap-modal');
}

async function openEditSapModal(id) {
  const data = await getJSON(`/sap/edit/${id}`);
  if (data.error) { showToast(data.error, 'error'); return; }
  document.getElementById('sap-id').value = data.id;
  document.getElementById('sap-name').value = data.transaction_name || '';
  document.getElementById('sap-code').value = data.transaction_code || '';
  document.getElementById('sap-movement').value = data.movement || '';
  document.getElementById('sap-modal-title').textContent = 'تعديل معاملة SAP';
  openModal('sap-modal');
}

async function saveSap() {
  const id = document.getElementById('sap-id').value;
  const form = document.getElementById('sap-form');
  const fd = new FormData(form);
  const url = id ? `/sap/edit/${id}` : '/sap/add';
  const data = await postForm(url, fd);
  if (data.success) { showToast(data.message); setTimeout(() => location.reload(), 800); }
  else showToast(data.message || 'حدث خطأ', 'error');
}

async function deleteSap(id, name) {
  if (!confirm(`هل تريد حذف معاملة "${name}"؟`)) return;
  const fd = new FormData();
  const data = await postForm(`/sap/delete/${id}`, fd);
  if (data.success) { showToast(data.message); setTimeout(() => location.reload(), 600); }
  else showToast('حدث خطأ', 'error');
}

// ── Other Users Module ───────────────────────────────────────────────────────
async function verifySecondaryAuth() {
  const pw = document.getElementById('secondary-password-input').value;
  if (!pw) { showToast('أدخل كلمة المرور الثانوية', 'error'); return; }
  const fd = new FormData();
  fd.append('secondary_password', pw);
  const data = await postForm('/secondary-auth', fd);
  if (data.success) { showToast('تم التحقق بنجاح'); setTimeout(() => location.reload(), 600); }
  else showToast(data.message || 'كلمة المرور غير صحيحة', 'error');
}

function openAddOtherUserModal() {
  document.getElementById('other-user-form')?.reset();
  document.getElementById('other-user-id').value = '';
  document.getElementById('other-user-modal-title').textContent = 'إضافة يوزر جديد';
  openModal('other-user-modal');
}

async function openEditOtherUserModal(id) {
  const data = await getJSON(`/other-users/edit/${id}`);
  if (data.error) { showToast(data.error, 'error'); return; }
  document.getElementById('other-user-id').value = data.id;
  document.getElementById('other-gpit').value = data.gpit || '';
  document.getElementById('other-username').value = data.username || '';
  document.getElementById('other-location').value = data.location || '';
  document.getElementById('other-user-modal-title').textContent = 'تعديل بيانات اليوزر';
  openModal('other-user-modal');
}

async function saveOtherUser() {
  const id = document.getElementById('other-user-id').value;
  const form = document.getElementById('other-user-form');
  const fd = new FormData(form);
  const url = id ? `/other-users/edit/${id}` : '/other-users/add';
  const data = await postForm(url, fd);
  if (data.success) { showToast(data.message); setTimeout(() => location.reload(), 800); }
  else showToast(data.message || 'حدث خطأ', 'error');
}

async function deleteOtherUser(id, name) {
  if (!confirm(`هل تريد حذف "${name}"؟`)) return;
  const fd = new FormData();
  const data = await postForm(`/other-users/delete/${id}`, fd);
  if (data.success) { showToast(data.message); setTimeout(() => location.reload(), 600); }
  else showToast('حدث خطأ', 'error');
}

// ── Cold Books Module ─────────────────────────────────────────────────────────
function calcSeqTo() {
  const seqFrom = parseInt(document.getElementById('book-seq-from').value);
  const pages   = parseInt(document.getElementById('book-pages').value);
  const qty     = parseInt(document.getElementById('book-quantity').value);
  const seqToEl = document.getElementById('book-seq-to');
  const summary = document.getElementById('seq-summary');
  if (!isNaN(seqFrom) && !isNaN(pages) && pages > 0 && !isNaN(qty) && qty > 0) {
    const seqTo        = seqFrom + (qty * pages);   // نهاية النطاق (غير شاملة آخر رقم)
    const totalSerials = qty * pages;
    seqToEl.value = seqTo;
    document.getElementById('seq-summary-text').textContent = `${seqFrom.toLocaleString('en')} → ${seqTo.toLocaleString('en')}`;
    document.getElementById('seq-total-serials').textContent = totalSerials.toLocaleString('en') + ' سيريال';
    summary.style.display = 'block';
  } else {
    seqToEl.value = '';
    summary.style.display = 'none';
  }
}

function openAddBookModal() {
  document.getElementById('book-form')?.reset();
  document.getElementById('book-id').value = '';
  document.getElementById('book-seq-to').value = '';
  document.getElementById('seq-summary').style.display = 'none';
  const minEl = document.getElementById('book-min-quantity');
  if (minEl) minEl.value = 5;
  document.getElementById('book-modal-title').textContent = 'إضافة دفتر جديد';
  openModal('book-modal');
}

async function openEditBookModal(id) {
  const data = await getJSON(`/cold-books/edit/${id}`);
  if (data.error) { showToast(data.error, 'error'); return; }
  document.getElementById('book-id').value = data.id;
  document.getElementById('book-name').value = data.book_name || '';
  document.getElementById('book-seq-from').value = data.seq_from || '';
  document.getElementById('book-pages').value = data.pages_per_book || '';
  document.getElementById('book-purchase-date').value = data.last_purchase_date || '';
  document.getElementById('book-quantity').value = data.quantity || '';
  document.getElementById('book-min-quantity').value = data.min_quantity || 5;
  document.getElementById('book-modal-title').textContent = 'تعديل بيانات الدفتر';
  calcSeqTo();
  openModal('book-modal');
}

async function saveBook() {
  const id   = document.getElementById('book-id').value;
  const name = document.getElementById('book-name').value.trim();
  if (!name) { showToast('يجب إدخال اسم الدفتر', 'error'); return; }

  const form = document.getElementById('book-form');
  const fd   = new FormData(form);
  // seq_to هو readonly فالـ FormData لا يأخذه تلقائياً — نضيفه يدوياً
  const seqToVal = document.getElementById('book-seq-to').value;
  if (seqToVal) fd.set('seq_to', seqToVal);

  const url  = id ? `/cold-books/edit/${id}` : '/cold-books/add';
  const data = await postForm(url, fd);
  if (data.success) { showToast(data.message); setTimeout(() => location.reload(), 800); }
  else showToast(data.message || 'حدث خطأ', 'error');
}

async function deleteBook(id, name) {
  if (!confirm(`هل تريد حذف الدفتر "${name}"؟`)) return;
  const fd = new FormData();
  const data = await postForm(`/cold-books/delete/${id}`, fd);
  if (data.success) { showToast(data.message); setTimeout(() => location.reload(), 600); }
  else showToast('حدث خطأ', 'error');
}

// ── Keyboard shortcuts ────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeAllModals();
});
