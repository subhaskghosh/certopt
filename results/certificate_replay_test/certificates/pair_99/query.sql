SELECT
  p.firstname,
  p.lastname,
  adr.city,
  adr.state
FROM PERSON AS P
LEFT JOIN ADDRESS AS ADR
  ON adr.personid = p.personid