**DualSync Pro: Windows 11 WSL Complete Setup Guide**

This guide will walk you through enabling the Windows Subsystem for Linux (WSL), 
installing the required dependencies, and launching DualSync Pro on Windows 11.


Step 1: Enable & Install WSL on Windows 11

  Before doing anything else, 
  you must install the Linux kernel sub-system on your Windows machine.
	How?
  Right-click the Windows Start Menu button and select Terminal (Admin) or Command Prompt (Admin).
  Type the following command and press Enter:
		
		cmdwsl --install
				
Restart your computer when prompted.
After restarting, a Linux Ubuntu terminal window will open automatically. 
It will ask you to create a username and password. 
Keep these safe!

Step 2: Update Your Linux Environment

  Open your newly installed Ubuntu terminal (you can find it anytime by searching "Ubuntu" in your Windows Start Menu) 
	and run this command to update its system catalogs:
  
    sudo apt update && sudo apt upgrade -y

Step 3: Install System Tools & FFmpeg

  DualSync Pro relies heavily on FFmpeg for audio warping and splitting drum components. 
  Run this command to install FFmpeg, Python tools, and build utilities
  
    sudo apt install ffmpeg build-essential python3-venv python3-pip -y

Step 4: Install Node.js (For the React Frontend)

Install the Node.js runtime environment via the NodeSource repository so your frontend can compile:

    curl -fsSL https://nodesource.com | sudo -E bash - 
    sudo apt install -y nodejs

Step 5: Clone and Set Up the Application

Clone the project repository directly into your Linux file space and open the folder:

    git clone https://github.com
    cd DualSync-Pro

Step 6: Configure Python Virtual Environment

Initialize an isolated Python sandbox to house the required libraries and AI modules safely:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt

(Note: The very first time you process a track inside the app later, 
it will pause for a couple of minutes to cleanly download the ~500MB Demucs AI voice separation models).

Step 7: Install Frontend UI Packages

Switch into the React UI directory and download its node modules:

    cd frontend
    npm install
    cd ..

    
🚀 Running the Application in WSL

Because WSL shares ports perfectly with Windows 11, you can launch the background instances 
directly from Linux and open them inside your native Windows web browser!

Terminal 1 (Start Backend API):

    source .venv/bin/activate
    python3 server.py


Terminal 2 (Open a new WSL tab, Start Frontend UI):

    cd DualSync-Pro/frontend
    npm run dev
    

Open your favorite Windows web browser (Chrome, Edge, Firefox) and navigate to: http://localhost:3000

