# Security Policy

## Reporting a Vulnerability

We take the security of AI Lead Qualifier seriously. If you discover a security vulnerability, please report it responsibly.

### How to Report

1. **Email**: Send a detailed report to security@syncta.ai
2. **Do NOT** open a public GitHub issue for security vulnerabilities
3. Include as much detail as possible:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

### Response Timeline

- **Acknowledgment**: Within 48 hours
- **Initial Assessment**: Within 5 business days
- **Resolution Target**: Within 30 days for critical issues

## Supported Versions

| Version | Supported |
| ------- | --------- |
| Latest  | Yes       |
| < 1.0   | No        |

## Security Measures

This project implements the following security practices:

- **Input Validation**: All API inputs are validated using Pydantic models
- **Rate Limiting**: API endpoints are rate-limited to prevent abuse
- **Authentication**: JWT-based authentication for all protected endpoints
- **Data Encryption**: Sensitive data is encrypted at rest and in transit
- **Dependency Scanning**: Automated vulnerability scanning via CI/CD pipeline
- **Secret Detection**: TruffleHog integration to prevent credential leaks
- **CORS Configuration**: Strict CORS policies for API access

## Security-Related Configuration

### Environment Variables

Never commit sensitive environment variables. Use `.env` files locally and secure secret management in production.

Required security-related environment variables:

```
JWT_SECRET=          # Strong random secret for JWT signing
API_KEY=             # API authentication key
REDIS_PASSWORD=      # Redis instance password
DATABASE_URL=        # Database connection (use SSL in production)
```

### Best Practices for Contributors

- Never hardcode secrets or API keys
- Use parameterized queries for database operations
- Keep dependencies updated
- Follow the principle of least privilege
- Write tests for security-critical code paths

## Disclosure Policy

We follow a coordinated disclosure process. We ask that you give us reasonable time to address vulnerabilities before public disclosure.
