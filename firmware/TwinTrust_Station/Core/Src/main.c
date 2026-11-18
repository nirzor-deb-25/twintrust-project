/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    main.c
  * @brief   TwinTrust Station - BME280 I2C reset diagnostics
  ******************************************************************************
  */
/* USER CODE END Header */

#include "main.h"

/* USER CODE BEGIN Includes */
#include <stdio.h>
#include <string.h>
/* USER CODE END Includes */

/* USER CODE BEGIN PD */
#define BME280_CHIP_ID_REG       0xD0U
#define BME280_EXPECTED_CHIP_ID  0x60U
/* USER CODE END PD */

I2C_HandleTypeDef hi2c1;
UART_HandleTypeDef huart3;

void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_USART3_UART_Init(void);
static void MX_I2C1_Init(void);

/* USER CODE BEGIN PFP */
static void UART_Print(const char *message);
static void Print_I2C_Status(void);
static void Reset_I2C1(void);
static void BME280_DetectionTest(void);
/* USER CODE END PFP */

/* USER CODE BEGIN 0 */

static void UART_Print(const char *message)
{
    HAL_UART_Transmit(
        &huart3,
        (uint8_t *)message,
        (uint16_t)strlen(message),
        500U
    );
}

static void Print_I2C_Status(void)
{
    char message[180];

    uint32_t sr1 = I2C1->SR1;
    uint32_t sr2 = I2C1->SR2;
    uint32_t cr1 = I2C1->CR1;

    snprintf(
        message,
        sizeof(message),
        "SCL=%u SDA=%u BUSY=%u HALstate=0x%02lX\r\n"
        "SR1=0x%08lX SR2=0x%08lX CR1=0x%08lX\r\n",
        (unsigned int)HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_8),
        (unsigned int)HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_9),
        (unsigned int)((sr2 & I2C_SR2_BUSY) != 0U),
        (unsigned long)HAL_I2C_GetState(&hi2c1),
        (unsigned long)sr1,
        (unsigned long)sr2,
        (unsigned long)cr1
    );

    UART_Print(message);
}

static void Reset_I2C1(void)
{
    if (HAL_I2C_DeInit(&hi2c1) != HAL_OK)
    {
        Error_Handler();
    }

    __HAL_RCC_I2C1_FORCE_RESET();
    HAL_Delay(1U);
    __HAL_RCC_I2C1_RELEASE_RESET();

    MX_I2C1_Init();

    HAL_Delay(10U);
}

static void BME280_DetectionTest(void)
{
    const uint8_t addresses[] = {0x76U, 0x77U};

    char message[180];
    uint8_t found = 0U;

    for (uint32_t i = 0U;
         i < (sizeof(addresses) / sizeof(addresses[0]));
         i++)
    {
        uint8_t address = addresses[i];
        uint16_t hal_address = (uint16_t)(address << 1U);
        uint8_t chip_id = 0U;

        HAL_StatusTypeDef status = HAL_I2C_IsDeviceReady(
            &hi2c1,
            hal_address,
            2U,
            100U
        );

        if (status != HAL_OK)
        {
            snprintf(
                message,
                sizeof(message),
                "Address 0x%02X: not ready; "
                "status=%u, error=0x%08lX\r\n",
                (unsigned int)address,
                (unsigned int)status,
                (unsigned long)HAL_I2C_GetError(&hi2c1)
            );

            UART_Print(message);

            UART_Print("Controller status after failed attempt:\r\n");
            Print_I2C_Status();

            continue;
        }

        status = HAL_I2C_Mem_Read(
            &hi2c1,
            hal_address,
            BME280_CHIP_ID_REG,
            I2C_MEMADD_SIZE_8BIT,
            &chip_id,
            1U,
            100U
        );

        if (status != HAL_OK)
        {
            snprintf(
                message,
                sizeof(message),
                "Address 0x%02X responds, but chip ID read failed; "
                "status=%u, error=0x%08lX\r\n",
                (unsigned int)address,
                (unsigned int)status,
                (unsigned long)HAL_I2C_GetError(&hi2c1)
            );

            UART_Print(message);
            Print_I2C_Status();

            continue;
        }

        if (chip_id == BME280_EXPECTED_CHIP_ID)
        {
            found = 1U;

            snprintf(
                message,
                sizeof(message),
                "BME280 FOUND: address=0x%02X, chip ID=0x%02X\r\n",
                (unsigned int)address,
                (unsigned int)chip_id
            );
        }
        else
        {
            snprintf(
                message,
                sizeof(message),
                "Address 0x%02X: chip ID=0x%02X; "
                "expected BME280 ID=0x60\r\n",
                (unsigned int)address,
                (unsigned int)chip_id
            );
        }

        UART_Print(message);
    }

    if (found == 0U)
    {
        UART_Print("BME280 has not been confirmed.\r\n");
    }

    UART_Print("----------------------------------------\r\n");
}

/* USER CODE END 0 */

int main(void)
{
    HAL_Init();

    SystemClock_Config();

    MX_GPIO_Init();
    MX_USART3_UART_Init();
    MX_I2C1_Init();

    /* USER CODE BEGIN 2 */
    HAL_Delay(100U);

    UART_Print("\r\n");
    UART_Print("TwinTrust Station: I2C RESET diagnostics\r\n");
    UART_Print("Configured pins: PB8=SCL, PB9=SDA\r\n");
    UART_Print("Pin levels: 1=HIGH, 0=LOW\r\n");
    UART_Print("----------------------------------------\r\n");
    /* USER CODE END 2 */

    /* USER CODE BEGIN WHILE */
    while (1)
    {
        HAL_GPIO_TogglePin(GPIOB, GPIO_PIN_0);

        Reset_I2C1();

        UART_Print("Controller status after reset:\r\n");
        Print_I2C_Status();

        BME280_DetectionTest();

        HAL_Delay(2000U);
    }
    /* USER CODE END WHILE */
}

void SystemClock_Config(void)
{
    RCC_OscInitTypeDef RCC_OscInitStruct = {0};
    RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

    __HAL_RCC_PWR_CLK_ENABLE();

    __HAL_PWR_VOLTAGESCALING_CONFIG(
        PWR_REGULATOR_VOLTAGE_SCALE1
    );

    RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
    RCC_OscInitStruct.HSEState = RCC_HSE_BYPASS;

    RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
    RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
    RCC_OscInitStruct.PLL.PLLM = 4;
    RCC_OscInitStruct.PLL.PLLN = 168;
    RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV2;
    RCC_OscInitStruct.PLL.PLLQ = 7;

    if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
    {
        Error_Handler();
    }

    RCC_ClkInitStruct.ClockType =
        RCC_CLOCKTYPE_HCLK |
        RCC_CLOCKTYPE_SYSCLK |
        RCC_CLOCKTYPE_PCLK1 |
        RCC_CLOCKTYPE_PCLK2;

    RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
    RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
    RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV4;
    RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV2;

    if (HAL_RCC_ClockConfig(
            &RCC_ClkInitStruct,
            FLASH_LATENCY_5
        ) != HAL_OK)
    {
        Error_Handler();
    }
}

static void MX_I2C1_Init(void)
{
    hi2c1.Instance = I2C1;

    hi2c1.Init.ClockSpeed = 100000;
    hi2c1.Init.DutyCycle = I2C_DUTYCYCLE_2;
    hi2c1.Init.OwnAddress1 = 0;
    hi2c1.Init.AddressingMode = I2C_ADDRESSINGMODE_7BIT;
    hi2c1.Init.DualAddressMode = I2C_DUALADDRESS_DISABLE;
    hi2c1.Init.OwnAddress2 = 0;
    hi2c1.Init.GeneralCallMode = I2C_GENERALCALL_DISABLE;
    hi2c1.Init.NoStretchMode = I2C_NOSTRETCH_DISABLE;

    if (HAL_I2C_Init(&hi2c1) != HAL_OK)
    {
        Error_Handler();
    }

    if (HAL_I2CEx_ConfigAnalogFilter(
            &hi2c1,
            I2C_ANALOGFILTER_ENABLE
        ) != HAL_OK)
    {
        Error_Handler();
    }

    if (HAL_I2CEx_ConfigDigitalFilter(&hi2c1, 0) != HAL_OK)
    {
        Error_Handler();
    }
}

static void MX_USART3_UART_Init(void)
{
    huart3.Instance = USART3;

    huart3.Init.BaudRate = 115200;
    huart3.Init.WordLength = UART_WORDLENGTH_8B;
    huart3.Init.StopBits = UART_STOPBITS_1;
    huart3.Init.Parity = UART_PARITY_NONE;
    huart3.Init.Mode = UART_MODE_TX_RX;
    huart3.Init.HwFlowCtl = UART_HWCONTROL_NONE;
    huart3.Init.OverSampling = UART_OVERSAMPLING_16;

    if (HAL_UART_Init(&huart3) != HAL_OK)
    {
        Error_Handler();
    }
}

static void MX_GPIO_Init(void)
{
    GPIO_InitTypeDef GPIO_InitStruct = {0};

    __HAL_RCC_GPIOC_CLK_ENABLE();
    __HAL_RCC_GPIOH_CLK_ENABLE();
    __HAL_RCC_GPIOA_CLK_ENABLE();
    __HAL_RCC_GPIOB_CLK_ENABLE();
    __HAL_RCC_GPIOD_CLK_ENABLE();
    __HAL_RCC_GPIOG_CLK_ENABLE();

    /*
     * NUCLEO-F439ZI LEDs:
     * PB0  = green
     * PB7  = blue
     * PB14 = red
     */
    HAL_GPIO_WritePin(
        GPIOB,
        GPIO_PIN_0 | GPIO_PIN_7 | GPIO_PIN_14,
        GPIO_PIN_RESET
    );

    GPIO_InitStruct.Pin =
        GPIO_PIN_0 | GPIO_PIN_7 | GPIO_PIN_14;

    GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
    GPIO_InitStruct.Pull = GPIO_NOPULL;
    GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;

    HAL_GPIO_Init(GPIOB, &GPIO_InitStruct);

    /*
     * PB8/PB9 I2C configuration and PD8/PD9 UART configuration
     * are provided by the existing stm32f4xx_hal_msp.c file.
     */
}

/* USER CODE BEGIN 4 */
/* USER CODE END 4 */

void Error_Handler(void)
{
    /* USER CODE BEGIN Error_Handler_Debug */
    __disable_irq();

    while (1)
    {
    }
    /* USER CODE END Error_Handler_Debug */
}

#ifdef USE_FULL_ASSERT
void assert_failed(uint8_t *file, uint32_t line)
{
    /* USER CODE BEGIN 6 */
    (void)file;
    (void)line;
    /* USER CODE END 6 */
}
#endif
