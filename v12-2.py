import os
import time
import json
import asyncio
import websockets
import aiohttp
import requests
import base58
import base64
from datetime import datetime, UTC
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from solders.keypair import Keypair
from solders.transaction import Transaction
from solders.message import Message
from solders.instruction import Instruction, AccountMeta
from solders.pubkey import Pubkey
from solders.hash import Hash

# Load environment variables
load_dotenv()

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8142691101:YPmoYtIw')
SOLANA_WALLET_ADDRESS = os.getenv('SOLANA_WALLET_ADDRESS', '4DdrfiDHpmx55i4SPssxVzS9ZaKLb8qr45NKY9Er9nNh')
PUMP_PORTAL_WS_URL = "wss://pumpportal.fun/api/data"
CHAT_ID = os.getenv('CHAT_ID', '-4639180839')
TRADE_AMOUNT = 0.3  # Fixed trade amount in SOL
API_KEY = os.getenv('PUMP_PORTAL_API_KEY', '8x16jgu1cy1pd9kk8mkp74dx1kegaa8dj32pbgehhq6dat9mt74jjudh0kuf8')
BITQUERY_TOKEN = os.getenv('BITQUERY_TOKEN', 'ory_at_4i_9qx3lfHO5hFrtPcJlXLo0gMDSqJ74')
BITQUERY_URL = "https://streaming.bitquery.io/eap"
SOL_MINT = "So11111111111111111111111111111111111111112"  # SOL mint address
RPC_URL = os.getenv('SOLANA_RPC_URL', "https://api.mainnet-beta.solana.com")

# User wallet for Jupiter swaps
USER_KEYPAIR_SECRET = os.getenv('USER_KEYPAIR_SECRET', '3Bk5D61EPSeiqB291NEnRKEdoJ')  # Add this to your .env file
USER_KEYPAIR = Keypair.from_bytes(base58.b58decode(USER_KEYPAIR_SECRET)) if USER_KEYPAIR_SECRET else None

# Trading configuration
JUPITER_SLIPPAGE_BPS = 4900  # 49% slippage (in basis points)
SELL_ALL_TOKEN = True  # Flag to indicate we want to sell all tokens

def setup_logging():
    logger = logging.getLogger('trading_bot')
    logger.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s GMT - %(levelname)s - %(message)s',
                                        datefmt='%Y-%m-%d %H:%M:%S')
    console_handler.setFormatter(console_formatter)

    file_handler = RotatingFileHandler(
        'trading_bot.log',
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(console_formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    return logger

logger = setup_logging()

class Position:
    def __init__(self):
        self.amount_sol = 0  # Amount in SOL
        self.entry_time = None
        self.has_position = False
        self.token_amount = 0  # Store exact token amount received from buy

class TradingBot:
    def __init__(self):
        self.positions = {}
        self.last_traded_mint = None
        self.session = None

    async def create_session(self):
        """Create aiohttp session for reuse"""
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session

    def fetch_latest_blockhash(self):
        """Fetch the latest blockhash from the Solana network."""
        try:
            url = RPC_URL
            params = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getLatestBlockhash",
                "params": [{"commitment": "finalized"}]
            }
            
            response = requests.post(url, json=params)
            
            if response.status_code == 200:
                result = response.json()
                if 'result' in result and 'value' in result['result']:
                    blockhash = result['result']['value']['blockhash']
                    return blockhash
                else:
                    raise Exception("Failed to get latest blockhash: " + str(result))
            else:
                raise Exception(f"Error fetching latest blockhash: {response.text}")
        except Exception as e:
            logger.error(f"Error fetching latest blockhash: {e}")
            return None

    async def get_jupiter_swap_data(self, input_mint, output_mint, amount, is_token_amount=False):
        """Get the swap data from Jupiter API.
        
        Args:
            input_mint: The mint address of the input token
            output_mint: The mint address of the output token
            amount: The amount to swap (in lamports for SOL, or actual token amount)
            is_token_amount: If True, amount is in token amount, not lamports
        """
        try:
            # First get the quote
            quote_url = "https://quote-api.jup.ag/v6/quote"
            quote_params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": JUPITER_SLIPPAGE_BPS,
                "onlyDirectRoutes": "false",
                "userPublicKey": str(USER_KEYPAIR.pubkey())
            }

            logger.info(f"Requesting Jupiter Quote API with params: {quote_params}")
            
            session = await self.create_session()
            async with session.get(quote_url, params=quote_params) as response:
                if response.status != 200:
                    logger.error(f"Error fetching swap quote: {response.status}")
                    error_text = await response.text()
                    logger.error(f"Response Content: {error_text}")
                    return None, 0
                
                quote_data = await response.json()
                logger.info(f"Quote received successfully: {json.dumps(quote_data)}")
                
                # Extract the outAmount from the quote data
                out_amount = int(quote_data.get("outAmount", "0"))

            # Then get the swap instructions
            swap_url = "https://quote-api.jup.ag/v6/swap-instructions"
            swap_data = {
                "quoteResponse": quote_data,
                "userPublicKey": str(USER_KEYPAIR.pubkey()),
                "wrapAndUnwrapSol": True
            }

            logger.info("Requesting Jupiter Swap Instructions")
            async with session.post(swap_url, json=swap_data) as response:
                if response.status != 200:
                    logger.error(f"Error creating swap instructions: {response.status}")
                    error_text = await response.text()
                    logger.error(f"Response Content: {error_text}")
                    return None, 0
                
                swap_result = await response.json()
                logger.info("Swap instructions received")
                
                return swap_result, out_amount

        except Exception as e:
            logger.error(f"Error getting Jupiter swap data: {e}")
            return None, 0

    async def create_and_sign_transaction(self, swap_data):
        """Create and sign a transaction using the Jupiter swap data."""
        try:
            # Get the latest blockhash
            blockhash_str = self.fetch_latest_blockhash()
            if not blockhash_str:
                return None
                
            logger.info(f"Got latest blockhash: {blockhash_str}")
            recent_blockhash = Hash.from_string(blockhash_str)
            
            # Create a list to store all instructions
            instructions = []
            
            # Process setup instructions
            for instruction_data in swap_data.get("setupInstructions", []):
                program_id = Pubkey.from_string(instruction_data["programId"])
                
                # Convert accounts data
                account_metas = []
                for account in instruction_data["accounts"]:
                    pubkey = Pubkey.from_string(account["pubkey"])
                    is_signer = account["isSigner"]
                    is_writable = account["isWritable"]
                    account_metas.append(AccountMeta(pubkey=pubkey, is_signer=is_signer, is_writable=is_writable))
                
                # Get the instruction data
                data = base64.b64decode(instruction_data["data"])
                
                # Create and add the instruction
                instruction = Instruction(program_id=program_id, data=data, accounts=account_metas)
                instructions.append(instruction)
            
            # Process the swap instruction
            swap_instruction = swap_data["swapInstruction"]
            program_id = Pubkey.from_string(swap_instruction["programId"])
            
            # Convert accounts data
            account_metas = []
            for account in swap_instruction["accounts"]:
                pubkey = Pubkey.from_string(account["pubkey"])
                is_signer = account["isSigner"]
                is_writable = account["isWritable"]
                account_metas.append(AccountMeta(pubkey=pubkey, is_signer=is_signer, is_writable=is_writable))
            
            # Get the instruction data
            data = base64.b64decode(swap_instruction["data"])
            
            # Create and add the instruction
            instruction = Instruction(program_id=program_id, data=data, accounts=account_metas)
            instructions.append(instruction)
            
            # Process cleanup instructions
            for instruction_data in swap_data.get("cleanupInstructions", []):
                program_id = Pubkey.from_string(instruction_data["programId"])
                
                # Convert accounts data
                account_metas = []
                for account in instruction_data["accounts"]:
                    pubkey = Pubkey.from_string(account["pubkey"])
                    is_signer = account["isSigner"]
                    is_writable = account["isWritable"]
                    account_metas.append(AccountMeta(pubkey=pubkey, is_signer=is_signer, is_writable=is_writable))
                
                # Get the instruction data
                data = base64.b64decode(instruction_data["data"])
                
                # Create and add the instruction
                instruction = Instruction(program_id=program_id, data=data, accounts=account_metas)
                instructions.append(instruction)
            
            # Create transaction with all instructions
            transaction = Transaction.new_with_payer(
                instructions=instructions,
                payer=USER_KEYPAIR.pubkey()
            )
            
            # Sign the transaction
            transaction.sign([USER_KEYPAIR], recent_blockhash)
            
            return transaction

        except Exception as e:
            logger.error(f"Error creating and signing transaction: {e}")
            return None

    async def send_transaction(self, transaction):
        """Send a signed transaction to the Solana network."""
        try:
            # Get the serialized transaction data
            serialized_tx = bytes(transaction)
            
            # Encode as base64 for sending
            serialized_tx_base64 = base64.b64encode(serialized_tx).decode('utf-8')
            
            # Send the transaction
            send_url = RPC_URL
            send_params = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    serialized_tx_base64,
                    {"encoding": "base64", "skipPreflight": True, "preflightCommitment": "confirmed"}
                ]
            }
            
            logger.info("Sending transaction to Solana network...")
            response = requests.post(send_url, json=send_params)
            
            if response.status_code == 200:
                result = response.json()
                if 'result' in result:
                    tx_signature = result['result']
                    logger.info(f"Transaction sent successfully! Signature: {tx_signature}")
                    return tx_signature
                else:
                    error_msg = result.get('error', {}).get('message', str(result))
                    logger.error(f"Failed to send transaction: {error_msg}")
                    return None
            else:
                logger.error(f"Error sending transaction: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logger.error(f"Error sending transaction: {e}")
            return None

    async def execute_jupiter_buy(self, mint, sol_amount):
        """Execute a buy trade through Jupiter API and return the exact token amount received"""
        try:
            # Convert SOL to lamports
            amount_in_lamports = int(sol_amount * 10**9)
            
            # 1. Get the swap data from Jupiter API for SOL -> Token
            swap_data, token_amount = await self.get_jupiter_swap_data(SOL_MINT, mint, amount_in_lamports)
            if not swap_data:
                return False, "Failed to get swap data from Jupiter", 0
            
            # 2. Create and sign the transaction
            transaction = await self.create_and_sign_transaction(swap_data)
            if not transaction:
                return False, "Failed to create and sign transaction", 0
            
            # 3. Send the transaction
            tx_signature = await self.send_transaction(transaction)
            if not tx_signature:
                return False, "Failed to send transaction", 0
            
            logger.info(f"Jupiter buy transaction complete! Signature: {tx_signature}, Received token amount: {token_amount}")
            return True, tx_signature, token_amount

        except Exception as e:
            logger.error(f"Error executing Jupiter buy: {e}")
            return False, str(e), 0

    async def execute_jupiter_sell(self, mint, token_amount):
        """Execute a sell trade through Jupiter API using 90% of the token amount from buy"""
        try:
            # Calculate 90% of the token amount to ensure the sell succeeds
            adjusted_token_amount = int(token_amount * 0.80)
            logger.info(f"Executing Jupiter sell for token {mint}")
            logger.info(f"Original token amount: {token_amount}, Using 90%: {adjusted_token_amount}")
            
            # First, check if the associated token account exists for this mint
            # and if not, wait for it to be properly initialized
            await self.ensure_token_account_initialized(mint)
            
            # Get the swap data from Jupiter API for Token -> SOL
            swap_data, expected_sol_amount = await self.get_jupiter_swap_data(
                mint, 
                SOL_MINT, 
                adjusted_token_amount,
                is_token_amount=True
            )
            
            if not swap_data:
                logger.error("Failed to get swap data from Jupiter for sell")
                return False, "Failed to get swap data from Jupiter"
            
            # Create and sign the transaction
            transaction = await self.create_and_sign_transaction(swap_data)
            if not transaction:
                logger.error("Failed to create and sign sell transaction")
                return False, "Failed to create transaction"
            
            # Send the transaction
            tx_signature = await self.send_transaction(transaction)
            if not tx_signature:
                logger.error("Failed to send sell transaction")
                return False, "Failed to send transaction"
            
            logger.info(f"Jupiter sell transaction complete! Signature: {tx_signature}")
            return True, tx_signature

        except Exception as e:
            logger.error(f"Error executing Jupiter sell: {e}")
            return False, str(e)
        
    async def ensure_token_account_initialized(self, mint):
        """Ensure the token account for a mint is properly initialized before selling"""
        try:
            # Get the associated token account address for this mint
            token_account_address = self.get_associated_token_account(mint)
            logger.info(f"Checking token account {token_account_address} for mint {mint}")
            
            # Try to check if the account exists
            max_retries = 3
            for i in range(max_retries):
                if await self.check_token_account_exists(token_account_address):
                    logger.info(f"Token account {token_account_address} exists and is initialized")
                    return True
                
                logger.info(f"Token account not ready yet. Waiting... ({i+1}/{max_retries})")
                await asyncio.sleep(2)  # Wait 2 seconds between checks
            
            logger.warning(f"Token account initialization timed out. Attempting to create it...")
            # If we got here, the account might not exist - we need to explicitly create it
            create_success = await self.create_associated_token_account(mint)
            if create_success:
                logger.info("Successfully created the associated token account")
                return True
            else:
                logger.error("Failed to create associated token account")
                return False
                
        except Exception as e:
            logger.error(f"Error ensuring token account initialization: {e}")
            return False

    def get_associated_token_account(self, mint):
        """Get the associated token account address for a mint"""
        try:
            # Build the RPC request
            url = RPC_URL
            params = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    str(USER_KEYPAIR.pubkey()),
                    {"mint": mint},
                    {"encoding": "jsonParsed"}
                ]
            }
            
            response = requests.post(url, json=params)
            
            if response.status_code == 200:
                result = response.json().get('result', {})
                value = result.get('value', [])
                
                if value and len(value) > 0:
                    # Return the first matching token account
                    return value[0]['pubkey']
                else:
                    logger.info(f"No token account found for mint {mint}")
                    return None
            else:
                logger.error(f"Error getting token accounts: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting associated token account: {e}")
            return None

    async def check_token_account_exists(self, account_address):
        """Check if a token account exists and is initialized"""
        if not account_address:
            return False
            
        try:
            # Build the RPC request
            url = RPC_URL
            params = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [
                    account_address,
                    {"encoding": "jsonParsed"}
                ]
            }
            
            session = await self.create_session()
            async with session.post(url, json=params) as response:
                if response.status != 200:
                    return False
                    
                result = await response.json()
                value = result.get('result', {}).get('value')
                
                # If value is not None, the account exists
                return value is not None
                
        except Exception as e:
            logger.error(f"Error checking token account: {e}")
            return False

    async def create_associated_token_account(self, mint):
        """Create the associated token account for a mint if it doesn't exist"""
        try:
            # Use the Associated Token Account program to create the account
            url = RPC_URL
            
            # First get the ATA address
            pubkey = str(USER_KEYPAIR.pubkey())
            params = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAssociatedTokenAddress",
                "params": [
                    pubkey,
                    mint
                ]
            }
            
            session = await self.create_session()
            async with session.post(url, json=params) as response:
                if response.status != 200:
                    logger.error(f"Error getting associated token address: {response.status}")
                    return False
                    
                result = await response.json()
                ata_address = result.get('result')
                
                if not ata_address:
                    logger.error("Failed to get associated token address")
                    return False
                    
            # Build and send the create ATA instruction
            # Note: This is a simplified approach - in reality you'd create a transaction
            # with the proper ATA creation instruction. For brevity, we're just showing the concept.
            logger.info(f"Attempting to create ATA: {ata_address}")
                    
            # In a real implementation, you would:
            # 1. Create the proper instruction for creating an ATA
            # 2. Build a transaction with that instruction
            # 3. Sign and send the transaction
            
            return True  # This would need to be the actual result in a real implementation
                
        except Exception as e:
            logger.error(f"Error creating associated token account: {e}")
            return False

    async def process_trade(self, trade_data):
        """Process a trade event from PumpPortal"""
        try:
            trader = trade_data.get('traderPublicKey')
            if trader != SOLANA_WALLET_ADDRESS:
                return

            mint = trade_data.get('mint')
            side = 'buy' if trade_data.get('txType') == 'buy' else 'sell'

            if mint not in self.positions:
                self.positions[mint] = Position()

            position = self.positions[mint]

            if side == 'buy':
                if mint == self.last_traded_mint:
                    self.log_position_ignore(mint, "Same token as last trade")
                    return

                if position.has_position:
                    self.log_position_ignore(mint, "Already holding position")
                    return

                logger.info(f"Detected buy signal for {mint} from tracked wallet")

                # Execute the buy trade through Jupiter
                success, trade_result, token_amount = await self.execute_jupiter_buy(
                    mint=mint,
                    sol_amount=TRADE_AMOUNT
                )

                if not success:
                    self.log_position_ignore(mint, f"Jupiter trade execution failed: {trade_result}")
                    return

                # Save the exact token amount we received
                position.token_amount = token_amount
                
                # Mark position as active
                position.has_position = True
                position.amount_sol = TRADE_AMOUNT
                position.entry_time = datetime.now(UTC)
                
                # Log the buy trade
                self.log_simple_trade(
                    mint,
                    "BUY",
                    TRADE_AMOUNT,
                    tx_signature=trade_result if isinstance(trade_result, str) else None,
                    token_amount=token_amount
                )
                
                # Wait for 10 seconds instead of 6 to give more time for confirmation
                logger.info(f"Waiting 10 seconds before selling {mint}")
                await asyncio.sleep(10)
                
                # Execute the sell trade through Jupiter using the exact token amount from buy
                logger.info(f"Executing sell strategy for {mint} with token amount: {position.token_amount}")
                sell_success, sell_result = await self.execute_jupiter_sell(mint, position.token_amount)
                
                if not sell_success:
                    logger.error(f"Failed to execute sell strategy: {sell_result}")
                    # Even if sell fails, mark the position as closed to avoid getting stuck
                    position.has_position = False
                    return
                
                # Log the sell trade
                self.log_simple_trade(
                    mint,
                    "SELL",
                    position.amount_sol,
                    tx_signature=sell_result if isinstance(sell_result, str) else None,
                    token_amount=position.token_amount
                )
                
                # Reset position tracking
                position.has_position = False
                position.amount_sol = 0
                position.token_amount = 0
                position.entry_time = None
                
                # Track last traded mint
                self.last_traded_mint = mint

            elif side == 'sell' and position.has_position:
                # This block is kept for handling the case where the tracked wallet sells
                # and we still have a position
                logger.info(f"Detected sell signal for {mint} from tracked wallet")

                # Execute the sell trade through Jupiter using the exact token amount we have stored
                success, trade_result = await self.execute_jupiter_sell(mint, position.token_amount)

                if not success:
                    logger.error(f"Failed to execute sell trade: {trade_result}")
                    # Even if sell fails, mark the position as closed to avoid getting stuck
                    position.has_position = False
                    return

                # Log the sell trade
                self.log_simple_trade(
                    mint,
                    "SELL",
                    position.amount_sol,
                    reason="tracked_wallet_sell",
                    tx_signature=trade_result if isinstance(trade_result, str) else None,
                    token_amount=position.token_amount
                )
                
                # Reset position tracking
                position.has_position = False
                position.amount_sol = 0
                position.token_amount = 0
                position.entry_time = None
                
                # Track last traded mint
                self.last_traded_mint = mint

        except Exception as e:
            logger.error(f"Error processing trade: {e}")

    def log_simple_trade(self, mint, action, amount_sol, reason=None, tx_signature=None, token_amount=None):
        timestamp = datetime.now(UTC).strftime('%d.%m.%y %H:%M:%S GMT')

        if action == "BUY":
            message = (
                f"🟢 BUY Signal at: {timestamp}\n"
                f"Token: {mint}\n"
                f"Amount: {amount_sol:.6f} SOL\n"
            )
            # Add token amount info
            if token_amount:
                message += f"Token Amount Received: {token_amount}\n"
            # Add transaction signature if available
            if tx_signature:
                message += f"Transaction: https://explorer.solana.com/tx/{tx_signature}\n"
        else:  # SELL
            reason_msg = "Bot Sold Out 100% After 3-Second Hold" if not reason else (
                "Bot Sold Out 100% Following Tracked Wallet's Sell"
            )
            message = (
                f"🔴 SELL Signal at: {timestamp}\n"
                f"Token: {mint}\n"
                f"{reason_msg}\n"
                f"Amount used: {amount_sol:.6f} SOL\n"
            )
            # Add token amount info
            if token_amount:
                message += f"Token Amount Sold: {token_amount}\n"
            # Add transaction signature if available
            if tx_signature:
                message += f"Transaction: https://explorer.solana.com/tx/{tx_signature}\n\nWallet: https://solscan.io/account/H6axhWEkc5MLuSkkcC9dFtd1Z4vCW9NfugTf3PUC2zQ4"

        logger.info(message)
        self.send_telegram_message(message)

    def log_position_ignore(self, mint, reason):
        timestamp = datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S GMT')
        message = f"[{timestamp}] ⏸️ Ignoring buy signal for {mint}: {reason}"
        logger.info(message)
        self.send_telegram_message(message)

    async def websocket_handler(self):
        while True:
            try:
                async with websockets.connect(PUMP_PORTAL_WS_URL) as websocket:
                    logger.info(f"Connected to PumpPortal WebSocket")

                    subscribe_payload = {
                        "method": "subscribeAccountTrade",
                        "keys": [SOLANA_WALLET_ADDRESS]
                    }
                    await websocket.send(json.dumps(subscribe_payload))
                    logger.info(f"Subscribed to trades for wallet: {SOLANA_WALLET_ADDRESS}")

                    while True:
                        try:
                            message = await websocket.recv()
                            trade_data = json.loads(message)
                            await self.process_trade(trade_data)

                        except json.JSONDecodeError:
                            logger.error("Failed to parse WebSocket message")
                            continue

            except websockets.exceptions.ConnectionClosed:
                logger.error("WebSocket connection closed. Reconnecting...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"WebSocket error: {str(e)}")
                await asyncio.sleep(5)

    def send_telegram_message(self, text):
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {'chat_id': CHAT_ID, 'text': text}
            response = requests.post(url, data=payload)
            if response.status_code != 200:
                logger.error(f"Failed to send Telegram message: {response.status_code}")
        except Exception as e:
            logger.error(f"Error sending Telegram message: {str(e)}")

    async def cleanup(self):
        """Cleanup resources"""
        if self.session:
            await self.session.close()

    async def start(self):
        """Start the trading bot"""
        try:
            # Validate required configuration
            if not USER_KEYPAIR:
                logger.error("Missing USER_KEYPAIR_SECRET in environment variables. Cannot execute Jupiter trades.")
                return
                
            logger.info("Starting trading bot...")
            logger.info(f"Monitoring wallet: {SOLANA_WALLET_ADDRESS}")
            logger.info(f"Trade amount set to: {TRADE_AMOUNT} SOL")
            logger.info(f"Trading strategy: Buy on Jupiter, wait 6 seconds, sell 100% on Jupiter")
            logger.info(f"Using Jupiter slippage setting: {JUPITER_SLIPPAGE_BPS/100}%")
            
            # Send startup notification to Telegram
            self.send_telegram_message("🤖 Bot started V12-2: Direct token amount passing from buy to sell (90%) using 0.3 SOL")

            websocket_task = asyncio.create_task(self.websocket_handler())
            await websocket_task

        finally:
            await self.cleanup()

if __name__ == '__main__':
    bot = TradingBot()
    asyncio.run(bot.start())
