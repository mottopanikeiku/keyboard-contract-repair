const nameField = document.querySelector('#display-name');
const saveButton = document.querySelector('#save-name');
const notificationButton = document.querySelector('#open-notifications');
const notificationDialog = document.querySelector('#notifications-dialog');
const doneButton = document.querySelector('#close-notifications');

saveButton.addEventListener('click', async (event) => {
  if (event.detail > 0) {
    await window.harbor.saveDisplayName();
  }
});

notificationButton.addEventListener('click', () => {
  notificationDialog.showModal();
});

doneButton.addEventListener('click', () => {
  notificationDialog.close();
});

notificationDialog.addEventListener('close', () => {
  nameField.focus();
});
