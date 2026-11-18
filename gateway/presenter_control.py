import json
import sys
import tkinter as tk
from pathlib import Path

state = Path(sys.argv[1])
root = tk.Tk()
root.title('TwinTrust - Presenter control')
root.geometry('520x260')
root.configure(bg='#0b1321')
paused = False

def save(value):
    temp = state.with_suffix('.tmp')
    temp.write_text(json.dumps({'paused': value}), encoding='utf-8')
    temp.replace(state)

def update():
    button.config(text='Resume live updates' if paused else 'Pause live updates')
    label.config(text=('Forwarding PAUSED\nDashboard becomes STALE after 5 seconds.' if paused else
                       'Forwarding ON\nDashboard becomes READY after fresh data passes checks.'))

def toggle():
    global paused
    try:
        save(not paused)
    except OSError as exc:
        label.config(text='Could not change forwarding:\n' + str(exc))
        return
    paused = not paused
    update()

def close():
    try:
        save(False)
    finally:
        root.destroy()

tk.Label(root, text='TwinTrust live-data demonstration', font=('Segoe UI', 17, 'bold'),
         bg='#0b1321', fg='white').pack(pady=18)
label = tk.Label(root, font=('Segoe UI', 11), bg='#0b1321', fg='white')
label.pack(pady=8)
button = tk.Button(root, command=toggle, font=('Segoe UI', 13, 'bold'), bg='#69ded2', padx=16, pady=10)
button.pack(pady=12)
tk.Label(root, text='Keep the board connected. Closing this window resumes forwarding.',
         bg='#0b1321', fg='#b8c9dc', font=('Segoe UI', 9)).pack()
root.protocol('WM_DELETE_WINDOW', close)
update()
root.mainloop()
